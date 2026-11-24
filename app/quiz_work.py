"""Explicit submission with a durable intent journal and record verification."""
import json
import os
import re
from pathlib import Path
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse, parse_qs
from bs4 import BeautifulSoup

from app.quiz_codec import decode_work_page, encode_form, work_identifiers
from app.retry import RetryNeeded, WorkUnavailable
from app.locking import file_lock


class Journal:
    def __init__(self, reporter):
        self.path = Path(reporter.payload.get("quiz_journal", "quiz-submissions.json"))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # A corrupt journal must never be treated as permission to resubmit.
        try:
            self.data = json.loads(self.path.read_text()) if self.path.exists() else {}
            if not isinstance(self.data, dict):
                raise ValueError()
        except (ValueError, OSError):
            raise RetryNeeded("本地交卷记录无法读取，保留记录并延迟核验，禁止重复提交") from None

    def save(self, key, value):
        # Different quizzes of one course may finish in different processes.
        # Merge under a lock instead of overwriting another process's intent.
        with file_lock(self.path.with_suffix('.lock')):
            try:
                latest = json.loads(self.path.read_text()) if self.path.exists() else {}
                if not isinstance(latest, dict):
                    raise ValueError()
            except (ValueError, OSError):
                raise RetryNeeded("本地交卷记录损坏，禁止覆盖及重复提交") from None
            latest[key] = value
            self.data = latest
            tmp = self.path.with_suffix(".tmp")
            with tmp.open("w", encoding="utf-8") as file:
                json.dump(latest, file, ensure_ascii=False)
                file.flush()
                os.fsync(file.fileno())
            os.replace(tmp, self.path)
            if os.name != 'nt':
                descriptor = os.open(self.path.parent, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)


def work_block_reason(html):
    soup = BeautifulSoup(html, "lxml")
    for element in soup.select("script,style,template,[hidden],.hidden,.hide"):
        element.decompose()
    for element in soup.select('[style]'):
        if element.attrs is None:
            continue
        if "display:none" in re.sub(r"\s+", "", element.get("style", "")).lower():
            element.decompose()
    text = soup.get_text(" ", strip=True)
    return next((reason for reason in ("已截止，不能作答", "已截止，不能提交", "此作业已过期", "已达到最大提交次数") if reason in text), None)


def redo_remaining(html):
    """Read an explicitly displayed remaining redo count; unknown stays None."""
    soup = BeautifulSoup(html, "lxml")
    labels = [element.get_text(" ", strip=True)
              for element in soup.select("button,a,[role='button']")
              if "重做" in element.get_text() or "再做" in element.get_text()]
    for label in labels:
        match = re.search(r"(?:剩余|还可|可再)\s*[:：]?\s*(\d+)\s*次", label)
        if match:
            return int(match.group(1))
    visible = " ".join(soup.stripped_strings)
    match = re.search(r"(?:重做|再做)[^。；;]{0,60}?(?:剩余|还可|可再)\s*[:：]?\s*(\d+)\s*次", visible)
    if match:
        return int(match.group(1))
    if "已达到最大提交次数" in visible:
        return 0
    return None


def record_detail(session, course, info, form, attempt):
    return session.get("https://mooc1.chaoxing.com/mooc-ans/work/record-detail", params={
        "courseId": course["courseId"], "classId": course["clazzId"], "workId": form["workId"],
        "workAnswerId": form.get("workAnswerId", ""), "cpi": course.get("cpi") or info.get("cpi", ""),
        "times": str(max(0, attempt - 1)), "api": "1", "mooc2": "1", "ut": "s",
        "isdisplaytable": "0", "firstHeader": "2", "isWork": "false", "workSystem": "0", "archive": "false"},
        timeout=(10, 25))


def record_is_submitted(session, course, info, form, attempt):
    response = session.get("https://mooc1.chaoxing.com/mooc-ans/work/record-detail", params={
        "courseId": course["courseId"], "classId": course["clazzId"], "workId": form["workId"],
        "workAnswerId": form.get("workAnswerId", ""), "cpi": course.get("cpi") or info.get("cpi", ""),
        # The list displays 第1次, while showRecord() sends zero-based times=0.
        "times": str(max(0, attempt - 1)), "api": "1", "mooc2": "1", "ut": "s",
        "isdisplaytable": "0", "firstHeader": "2", "isWork": "false", "workSystem": "0", "archive": "false"}, timeout=(10, 25))
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "lxml")
    for element in soup.select("script,style"):
        element.decompose()
    text = soup.get_text(" ", strip=True)
    stamp = re.search(r"提交时间\s*[：:]\s*\d{4}[-/.年]\s*\d{1,2}[-/.月]\s*\d{1,2}", text)
    # A reserved 第1次/0分 row with an empty timestamp is NOT a submission.
    return bool(stamp)


def records(session, course, info, form):
    from api.base import _parse_work_record_list
    response = session.get("https://mooc1.chaoxing.com/mooc-ans/work/record-list", params={
        "courseId": course["courseId"], "classId": course["clazzId"], "workId": form["workId"],
        "workAnswerId": form.get("workAnswerId", ""), "cpi": course.get("cpi") or info.get("cpi", ""),
        "api": "1", "mooc2": "1", "ut": "s"}, timeout=(10, 25))
    response.raise_for_status()
    # An empty record list is not proof of completion.
    parsed = _parse_work_record_list(response.text)
    # The browser fills the detail's empty timestamp from the list row's `data`
    # attribute. Bind that timestamp to the same displayed attempt, never a
    # neighbouring row or a reserved 第1次/0分 entry without a real date.
    dated_attempts = set()
    soup = BeautifulSoup(response.text, 'lxml')
    for row in soup.select('.viewMenuList li'):
        label = row.select_one('.viewNum')
        match = re.fullmatch(r'第\s*(\d+)\s*次', label.get_text(strip=True)) if label else None
        stamp = row.get('data', '').strip()
        if not match or not re.fullmatch(r'\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}', stamp):
            continue
        try:
            datetime.strptime(stamp, '%Y-%m-%d %H:%M:%S')
        except ValueError:
            continue
        dated_attempts.add(int(match[1]))
    scores = re.findall(r'viewScore">([\d.]+)分', response.text)
    candidates = parsed if len(scores) == len(parsed) else [(attempt, None) for attempt, _ in parsed]
    return [(attempt, score) for attempt, score in candidates
            if attempt in dated_attempts or record_is_submitted(session, course, info, form, attempt)]


def completion_url(value, course, job):
    """Accept only the current task's normal platform success navigation."""
    target = urlparse(urljoin('https://mooc1.chaoxing.com', value))
    params = parse_qs(target.query)
    if (target.scheme != 'https' or target.hostname not in ('mooc1.chaoxing.com', 'mooc1-1.chaoxing.com')
            or target.path != '/mooc-ans/api/work'
            or params.get('courseid') != [str(course['courseId'])]
            or params.get('clazzId') != [str(course['clazzId'])]
            or params.get('jobid') != [str(job['jobid'])]
            or params.get('submit') != ['true']):
        raise WorkUnavailable('交卷后的确认链接与当前任务不匹配，未访问未知地址')
    return target.geturl()


def visit_completion(session, course, job, entry, journal, reporter):
    if not entry.get('completion_url') or entry.get('completion_visited'):
        return
    target = completion_url(entry['completion_url'], course, job)
    try:
        response = session.get(target, timeout=(10, 25), allow_redirects=False)
        response.raise_for_status()
        if response.status_code not in (200, 301, 302, 303, 307, 308):
            raise ValueError('unexpected completion response')
        if response.status_code != 200:
            location = response.headers.get('Location', '')
            destination = urlparse(urljoin(target, location))
            if (not location or destination.scheme != 'https'
                    or destination.hostname not in ('mooc1.chaoxing.com', 'mooc1-1.chaoxing.com')
                    or '/login' in destination.path):
                raise ValueError('completion redirected outside course')
    except Exception:
        raise RetryNeeded('交卷接口已返回成功，提交后确认页暂时不可用；稍后只重试确认，不重复交卷') from None
    journal.save(str(job['jobid']), {**entry, 'completion_visited': True})
    reporter.log('已访问平台提交后确认页，任务点完成状态由课程回读核验')


def save_submit_response(result, detail=None):
    """Store raw bounded response privately, including failures before parsing."""
    detail = dict(detail or {})
    if result is not None:
        content_type = result.headers.get('Content-Type')
        detail.update(http_status=result.status_code if isinstance(result.status_code, int) else None,
                      content_type=content_type if isinstance(content_type, str) else None,
                      response_body=result.text[:131072] if isinstance(result.text, str) else '')
    path = Path('submit-response.json')
    descriptor = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as file:
        json.dump(detail, file, ensure_ascii=False)


def submission_diagnostic(result, error, reporter):
    """Keep evidence even when the HTTP guard raised before returning a response."""
    try:
        detail = json.loads(Path('submit-response.json').read_text()) if result is None else {}
    except (OSError, ValueError):
        detail = {}
    if not isinstance(detail, dict):
        detail = {}
    detail.update(at=datetime.now(timezone.utc).isoformat(), exception_type=type(error).__name__,
                  error=reporter.clean(str(error)))
    save_submit_response(result, detail)
    if result is not None:
        content_type = result.headers.get('Content-Type')
        detail.update(http_status=result.status_code if isinstance(result.status_code, int) else None,
                      content_type=content_type if isinstance(content_type, str) else None)
    return {key: value for key, value in detail.items() if key != 'response_body'}


def execute_work(client, course, job, info, reporter):
    from api.base import SessionManager, StudyResult
    session = SessionManager.get_session()
    journal = Journal(reporter)
    key = str(job["jobid"])
    entry = journal.data.get(key)
    redo_from = None
    permits = None
    if reporter.payload.get('point_store'):
        from app.quiz_redo import RedoPermits
        permits = RedoPermits(reporter.payload['point_store'])
    if entry and (not isinstance(entry, dict) or entry.get("phase") not in ("intent", "confirmed", "rejected") or not isinstance(entry.get("form"), dict) or not entry["form"].get("workId")):
        raise RetryNeeded("本地交卷记录结构异常，保留记录并延迟核验，禁止重复提交")
    if entry and entry["phase"] in ("intent", "confirmed"):
        # Match the real page's success navigation before querying records.
        # Some platform updates happen only when this GET is visited.
        visit_completion(session, course, job, entry, journal, reporter)
        entry = journal.data[key]
        form = entry["form"]
        try:
            found = records(session, course, info, form)
        except Exception:
            raise RetryNeeded("已发起交卷，成绩查询暂不可用；稍后只核验记录，不重复交卷") from None
        if entry.get('redo_from'):
            found = [record for record in found if record[0] > entry['redo_from']['attempt']]
        if not found:
            raise RetryNeeded("已发起交卷，平台尚未确认作答记录；稍后只核验记录，不重复交卷")
        attempt, score = max(found, key=lambda record: record[0])
        confirmed = {**entry, "phase": "confirmed", "score": score}
        journal.save(key, confirmed)
        remaining = None
        # Existing, explicitly authorized redo flow reads this same detail page
        # below; avoid an extra request before it obtains the feedback.
        if not (permits and permits.available(key, attempt)):
            try:
                detail = record_detail(session, course, info, form, attempt)
                detail.raise_for_status()
                remaining = redo_remaining(detail.text)
            except Exception:
                reporter.log("平台重做次数暂不可读取；成绩记录已确认", "warning")
        reporter.update(quiz_result={"status": "submitted", "score": score, "attempt": attempt,
                                     "redo_remaining": remaining})
        if permits and score is not None and score < 99.95 and permits.available(key, attempt):
            redo_from = {'attempt': attempt, 'score': score, 'prior_submission': confirmed}
            reporter.log(f'平台正式成绩 {score} 分、任务点仍待完成；按用户授权准备额外重做一次')
        else:
            reporter.log(f"平台已有正式作答记录，成绩 {score if score is not None else '待批阅'}；保留本次成绩，不自动重做")
            if reporter.payload.get('action') == 'job' and score is not None and score < 99.95:
                raise WorkUnavailable(f'测验已有正式成绩 {score} 分，但平台任务点仍未通过；未获额外重做授权或本次一次性授权已使用，不重复交卷')
            return StudyResult.SUCCESS

    response = session.get("https://mooc1.chaoxing.com/mooc-ans/api/work", params={
        "api": "1", "workId": key.replace("work-", ""), "jobid": key, "originJobId": key,
        "needRedirect": "true", "skipHeader": "true", "knowledgeid": str(info["knowledgeid"]),
        "ktoken": info["ktoken"], "cpi": info["cpi"], "ut": "s", "clazzId": course["clazzId"],
        "type": "", "enc": job["enc"], "mooc2": "1", "courseid": course["courseId"]}, timeout=(10, 25))
    response.raise_for_status()
    minimal = work_identifiers(response.text, key.replace("work-", ""))
    blocked = work_block_reason(response.text)
    if blocked:
        raise WorkUnavailable(f"平台限制：{blocked}；未调用答题服务、未交卷")
    if "教师未创建完成该测验" in response.text:
        raise RetryNeeded("教师尚未创建完成测验，稍后重试")
    # Existing records are formal attempts; never consume another attempt automatically.
    existing = records(session, course, info, minimal)
    if existing and redo_from is None:
        journal.save(key, {"phase": "confirmed", "form": minimal})
        return execute_work(client, course, job, info, reporter)
    if redo_from and (not existing or max(record[0] for record in existing) != redo_from['attempt']):
        raise RetryNeeded('作答记录已变化，重新核验后再决定是否使用一次性重做授权')
    try:
        form = decode_work_page(response.text, response.url if isinstance(response.url, str) else "https://mooc1.chaoxing.com/mooc-ans/work/doHomeWorkNew")
        if not form.get("questions"):
            raise ValueError("未读取到完整题目")
        corrections = {}
        if redo_from:
            previous = redo_from['prior_submission']['form']
            detail = session.get('https://mooc1.chaoxing.com/mooc-ans/work/record-detail', params={
                'courseId': course['courseId'], 'classId': course['clazzId'], 'workId': previous['workId'],
                'workAnswerId': previous.get('workAnswerId', ''), 'cpi': course.get('cpi') or info.get('cpi', ''),
                'times': str(redo_from['attempt'] - 1), 'api': '1', 'mooc2': '1', 'ut': 's',
                'isdisplaytable': '0', 'firstHeader': '2', 'isWork': 'false', 'workSystem': '0', 'archive': 'false'}, timeout=(10, 25))
            detail.raise_for_status()
            from app.question_dom import feedback_answers
            corrections = feedback_answers(detail.text, form['questions'])
            reporter.log(f'按题号与完整选项内容核对平台反馈，取得 {len(corrections)}/{len(form["questions"])} 题明确更正答案')
        unresolved = [q for q in form['questions'] if str(q['id']) not in corrections]
        if any(q.get("attachments") or q.get("image_sources") for q in unresolved):
            from app.materials import prepare_questions
            prepare_questions(unresolved, session, response.url, reporter.payload.get("quiz_config", {}), reporter)
        generated = iter(client.tiku.resolve(unresolved) if unresolved else [])
        answers = [corrections[str(q['id'])] if str(q['id']) in corrections else next(generated) for q in form['questions']]
        data = encode_form(form, answers)
    except (ValueError, AttributeError, TypeError, KeyError):
        raise RetryNeeded("题目、答案或表单结构未通过校验；未交卷，稍后重试") from None
    reporter.checkpoint()
    identifiers = minimal
    intent = {"phase": "intent", "form": identifiers}
    if redo_from:
        if not permits.consume(key, redo_from['attempt']):
            raise WorkUnavailable('本份测验的一次性重做授权已使用，未再次交卷')
        intent['redo_from'] = redo_from
    # Persist BEFORE the request. A crash/timeout may happen after server acceptance.
    journal.save(key, intent)
    reporter.log("答案格式已核验，正在正式提交测验（不保存草稿）")
    result = None
    try:
        result = session.post(form["__submit_url"], data=data,
            headers={"X-Requested-With": "XMLHttpRequest", "Origin": "https://mooc1.chaoxing.com",
                     "Referer": response.url if isinstance(response.url, str) else "https://mooc1.chaoxing.com/"},
            timeout=(10, 35), allow_redirects=False)
        save_submit_response(result)
        result.raise_for_status()
        body = result.json()
        if not isinstance(body, dict):
            raise ValueError('交卷接口返回的 JSON 不是对象')
        if body.get("status") is False:
            # Explicit rejection is safe to retry after fetching fresh form tokens.
            journal.save(key, {"phase": "rejected", "form": identifiers})
            reason = reporter.clean(str(body.get("msg") or "未提供原因"))
            if any(marker in reason for marker in ("已过期", "已截止", "已达到最大提交次数")):
                raise WorkUnavailable(f"平台明确拒绝交卷：{reason}；需要教师重新开放或调整课程设置")
            raise RetryNeeded(f"平台明确拒绝交卷：{reason}；稍后重新读取题目和表单后重试")
        if body.get("status") is not True or ("保存" in str(body.get("msg", "")) and "提交" not in str(body.get("msg", ""))):
            raise RetryNeeded("交卷响应未明确确认提交，稍后核验作答记录")
        if isinstance(body.get('url'), str) and body['url']:
            target = completion_url(body['url'], course, job)
            journal.save(key, {**journal.data[key], 'completion_url': target})
    except WorkUnavailable:
        raise
    except RetryNeeded:
        raise
    except Exception as error:
        detail = submission_diagnostic(result, error, reporter)
        journal.save(key, {**journal.data[key], 'submit_diagnostic': detail})
        reporter.update(submit_diagnostic=detail)
        raise RetryNeeded(f"交卷响应异常（{type(error).__name__}）；稍后核验作答记录，不重复提交") from None
    return execute_work(client, course, job, info, reporter)
