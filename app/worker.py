"""One account/course per isolated local process."""
import functools
import json
import os
import re
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from datetime import timedelta
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
if __name__ == '__main__':
    # The coordinator imports this module as app.worker. Keep exception and
    # cancellation classes identical when the script is launched by filename.
    sys.modules['app.worker'] = sys.modules[__name__]
from app.state_io import write_json
from app.retry import RetryNeeded, WorkUnavailable, PointRecoveryNeeded


def timestamp():
    return datetime.now(timezone.utc).isoformat()


class NeedsAttention(Exception):
    pass


class Cancelled(BaseException):
    pass


class Reporter:
    def __init__(self, payload):
        self.payload = payload
        self.path = Path("state.json")
        self.lock = threading.RLock()
        self.state = {**payload.get("previous_state", {}), "status": "running", "pending_quiz": None, "error": None, "progress": payload.get("previous_state", {}).get("progress", 0),
                      "done": payload.get("previous_state", {}).get("done", 0), "total": payload.get("previous_state", {}).get("total", 0),
                      "chapter": "正在连接学习通", "logs": list(payload.get("previous_state", {}).get("logs", [])), "updated_at": timestamp(),
                      "started_at": timestamp(), "pid": os.getpid()}
        self.remote = None
        self.remote_control = "run"
        self.stop = threading.Event()
        self.flush()

    def clean(self, text):
        text = str(text)
        for value in [self.payload.get('session_cookies'), *self.payload.get("credentials", {}).values(),
                      *[self.payload.get("quiz_config", {}).get(key) for key in ("key", "tokens", "go_authorization", "like_tokens", "siliconflow_key")]]:
            if value:
                text = text.replace(str(value), "[已隐藏]")
        text = re.sub(r"https?://\S+", "[链接已隐藏]", text)
        text = re.sub(r"(?i)(cookie|token|password|authorization)\s*[:=].*", r"\1=[已隐藏]", text)
        return text[:600]

    def update(self, **kwargs):
        with self.lock:
            self.state.update(kwargs, updated_at=timestamp())
            self.flush()

    def log(self, message, level="info"):
        with self.lock:
            self.state["logs"].append({"time": timestamp(), "level": level, "message": self.clean(message)})
            self.state["logs"] = self.state["logs"][-200:]
            self.flush()

    def flush(self):
        with self.lock:
            write_json(self.path, self.state)

    def checkpoint(self):
        if os.name == 'nt' and self.payload.get('action') == 'job' and self.payload.get('unit') and self.payload.get('claim_owner'):
            now = time.monotonic()
            if now - getattr(self, '_claim_heartbeat', 0) >= 10:
                from app.point_queue import PointLedger
                PointLedger(self.payload['point_store']).renew(
                    self.payload['unit']['id'], self.payload['claim_owner'], os.getpid())
                self._claim_heartbeat = now
        while True:
            parent = self.payload.get("parent_pid")
            if parent:
                if os.name == "nt":
                    import psutil
                    if not psutil.pid_exists(parent):
                        raise Cancelled()
                elif os.getppid() != parent:
                    raise Cancelled()
            try:
                action = json.loads(Path(self.payload.get('control_path', 'control.json')).read_text())["action"]
            except (OSError, ValueError, KeyError):
                action = "run"
            if action == "cancel":
                raise Cancelled()
            if action != "pause":
                return
            time.sleep(0.5)


def platform_client(payload, reporter):
    sys.path.insert(0, str(ROOT / "vendor" / "chaoxing"))
    # The untouched upstream resolves its bundled font table relative to cwd.
    import shutil
    Path("resource").mkdir(exist_ok=True)
    shutil.copyfile(ROOT / "vendor" / "chaoxing" / "resource" / "font_map_table.json", "resource/font_map_table.json")
    # Imported lazily: demo mode does not touch the learning platform.
    from api.base import Account, Chaoxing
    from api.logger import logger
    import api.base as base
    import requests
    from tqdm import tqdm

    logger.remove()
    # Persist our own messages only. Upstream logs can contain response HTML/cookies.
    original_request = requests.Session.request

    @functools.wraps(original_request)
    def request(session, method, url, **kwargs):
        reporter.checkpoint()
        kwargs.setdefault("timeout", (10, 25))
        learning_host = urlparse(url).hostname in ("mooc1.chaoxing.com", "mooc1-1.chaoxing.com", "mooc2-ans.chaoxing.com", "passport2.chaoxing.com", "i.chaoxing.com")
        if learning_host and payload.get('request_gate'):
            from app.point_queue import RequestGate
            RequestGate(payload['request_gate']).wait(reporter)
        try:
            response = original_request(session, method, url, **kwargs)
        except requests.RequestException as error:
            reporter.update(last_request_failure={'host': urlparse(url).hostname,
                                                  'type': type(error).__name__})
            raise
        if (learning_host and method.upper() == 'POST'
                and urlparse(url).path in ('/mooc-ans/work/addStudentWorkNew', '/mooc-ans/work/addStudentWorkNewWeb')):
            from app.quiz_work import save_submit_response
            save_submit_response(response)
        if learning_host and (response.status_code in (401, 403) or "passport2.chaoxing.com/login" in response.url):
            raise NeedsAttention("登录过期或出现验证，请在学习通手动处理后重试")
        from app.verification import requires_verification
        if learning_host and requires_verification(response):
            raise NeedsAttention("平台要求手动验证，请在学习通处理后重试")
        return response

    requests.Session.request = request

    class Progress(tqdm):
        def __init__(self, *args, **kwargs):
            kwargs["disable"] = True
            super().__init__(*args, **kwargs)

        def refresh(self, *args, **kwargs):
            reporter.checkpoint()
            if self.total:
                reporter.update(video_progress=round(100 * self.n / self.total, 1),
                                video_seconds=int(self.n), video_total=int(self.total))

    base.tqdm = Progress
    credentials = payload["credentials"]
    from app.quiz import build_tiku
    tiku = build_tiku(payload.get("quiz_config", {}), reporter, NeedsAttention) if payload.get("action") in ("learn", "job") else None
    client = Chaoxing(account=Account(credentials["username"], credentials["password"]),
                      tiku=tiku, work_redo_enabled=False, query_delay=1)
    if payload.get('action') == 'job':
        progress_log = client.video_progress_log
        def verified_video_progress(*args, **kwargs):
            result = progress_log(*args, **kwargs)
            if result[0] is True:
                reporter.update(platform_point_completed=True)
            return result
        client.video_progress_log = verified_video_progress
    if payload.get('session_cookies'):
        # A child reuses its already authenticated parent's session. Do not
        # perform simultaneous password logins for three points of one account.
        fd = os.open('cookies.txt', os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as cookie_file:
            cookie_file.write(payload['session_cookies'])
        result = {'status': True}
    else:
        result = client.login()
    if not result.get("status"):
        raise NeedsAttention("登录失败：" + reporter.clean(result.get("msg", "请检查账号密码")))
    if tiku:
        from app.quiz_work import execute_work
        client.study_work = lambda course, job, info: execute_work(client, course, job, info, reporter)
    return client


def course_key(course):
    return f'{course["courseId"]}:{course["clazzId"]}:{course.get("cpi", "")}'


def demo_courses():
    return [{"courseId": str(i + 1001), "clazzId": "202601", "cpi": "demo",
             "title": title, "teacher": teacher, "total": total, "done": done}
            for i, (title, teacher, total, done) in enumerate([
                ("大学英语 · 读写进阶", "外国语学院", 12, 3), ("人工智能与未来生活", "通识教育中心", 16, 0),
                ("中国传统文化", "人文学院", 10, 6), ("大学生心理健康", "心理教育中心", 8, 8)])]


def scan(payload, reporter):
    if payload["mode"] == "demo":
        return demo_courses()
    client = platform_client(payload, reporter)
    courses = client.get_course_list()
    seen = set()
    result = []
    for course in courses:
        key = course_key(course)
        if key not in seen:
            result.append(course)
            seen.add(key)
    return result


def inspect_progress(payload, reporter):
    from app.progress import demo_progress, inspect_course
    course = payload["course"]
    if payload["mode"] == "demo":
        return demo_progress(course)
    client = platform_client(payload, reporter)
    if not any(course_key(c) == course_key(course) for c in client.get_course_list()):
        raise NeedsAttention("课程已不可用，请先同步课程列表")
    from api.base import SessionManager
    completed_keys = set()
    if payload.get('point_store') and Path(payload['point_store']).exists():
        import sqlite3
        with sqlite3.connect(f"file:{payload['point_store']}?mode=ro", uri=True) as connection:
            completed_keys = {row[0] for row in connection.execute("SELECT id FROM points WHERE status='completed'")}
    return inspect_course(client, course, SessionManager.get_session(), reporter, completed_keys)


def learn(payload, reporter):
    if 'point_concurrency' in payload:
        from app.parallel import learn_parallel
        return learn_parallel(payload, reporter)
    course = payload["course"]
    if payload["mode"] == "demo":
        if os.environ.get("STUDY_DEMO_RETRY_ONCE") == "1" and not payload.get("previous_state", {}).get("retry_count"):
            raise RetryNeeded("演示：模拟答题接口返回无效 JSON，稍后自动恢复")
        total, done = course["total"], course["done"]
        reporter.log("演示任务已启动；所有进度均为本地模拟，不会访问学习通")
        reporter.update(total=total, done=done, progress=round(done / total * 100))
        if os.environ.get("STUDY_DEMO_QUIZ") == "1":
            from app.quiz import build_tiku
            tiku = build_tiku({"provider": "manual", "mode": "assisted"}, reporter, NeedsAttention)
            tiku.query_all([{"id": "demo-question", "title": "演示题：1 + 1 等于几？", "type": "single",
                             "options": "A. 1\nB. 2\nC. 3"}])
            reporter.log("演示答案已接收，继续模拟学习；未向学习通提交")
        for index in range(done, total):
            reporter.update(chapter=f"第 {index + 1} 节 · 演示课程内容")
            for second in range(10):
                reporter.checkpoint()
                reporter.update(video_progress=(second + 1) * 10, video_seconds=second + 1, video_total=10)
                time.sleep(float(os.environ.get("DEMO_TICK_SECONDS", "1")))
            reporter.log(f"第 {index + 1} 节演示完成")
            reporter.update(done=index + 1, progress=round((index + 1) / total * 100))
        reporter.update(status="completed", chapter="演示任务已完成", progress=100)
        return

    client = platform_client(payload, reporter)
    reporter.log("已登录，正在读取课程章节")
    # Revalidate the exact class. Never fall back to all courses on a selection miss.
    matches = [c for c in client.get_course_list() if course_key(c) == course_key(course)]
    if not matches:
        raise NeedsAttention("所选课程已不可用，请重新同步课程")
    course = matches[0]
    points = client.get_course_point(course["courseId"], course["clazzId"], course["cpi"])["points"]
    if not points:
        raise NeedsAttention("未读取到章节，可能是课程未开放或平台接口发生变化")
    total = len(points)
    done = sum(bool(p.get("has_finished")) for p in points)
    reporter.update(total=total, done=done, progress=round(done / total * 100))
    blocked = []
    for point in points:
        reporter.checkpoint()
        if point.get("has_finished"):
            continue
        reporter.update(chapter=point["title"], video_progress=0)
        reporter.log("正在处理章节：" + point["title"])
        jobs, info = client.get_job_list(course, point)
        if info.get("notOpen"):
            blocked.append(point["title"])
            reporter.log("章节未开放，已保留待处理", "warning")
            continue
        for job in jobs:
            reporter.checkpoint()
            kind = job.get("type")
            reporter.log("正在处理：" + str(job.get("name") or kind))
            if kind == "video":
                result = client.study_video(course, job, info, _speed=1.0)
                if result.is_failure():
                    result = client.study_video(course, job, info, _speed=1.0, _type="Audio")
            elif kind == "document":
                result = client.study_document(course, job)
            elif kind == "read":
                result = client.study_read(course, job, info)
            elif kind == "workid":
                reporter.log("开始处理章节测验；未取得有效答案时按运行模式重试或等待填写")
                result = client.study_work(course, job, info)
            else:
                reporter.log("此任务点需手动完成：" + str(kind), "warning")
                blocked.append(point["title"])
                continue
            if result.is_failure():
                raise NeedsAttention("任务点处理失败，请在学习通检查课程与验证状态")
        # Re-read authoritative completion instead of treating a successful HTTP call as completion.
        verified = client.get_course_point(course["courseId"], course["clazzId"], course["cpi"])["points"]
        if not verified:
            raise NeedsAttention("无法核验平台章节进度，请同步后重试")
        done = sum(bool(p.get("has_finished")) for p in verified)
        reporter.update(done=done, progress=round(done / total * 100, 1))
    verified = client.get_course_point(course["courseId"], course["clazzId"], course["cpi"])["points"]
    if not verified:
        raise NeedsAttention("无法核验最终进度，请重新同步课程")
    done = sum(bool(p.get("has_finished")) for p in verified)
    if done < len(verified):
        raise RetryNeeded(f"平台确认 {done}/{len(verified)} 节完成，其余尚未完成，稍后重新核验")
    reporter.log("平台已确认所有章节完成")
    reporter.update(status="completed", done=done, total=len(verified), progress=100, chapter="课程已完成")


def execute_job(client, course, unit, reporter):
    job, info = unit['job'], unit['info']
    kind = job.get('type')
    if kind == 'video':
        result = client.study_video(course, job, info, _speed=1.0)
        if result.is_failure():
            result = client.study_video(course, job, info, _speed=1.0, _type='Audio')
    elif kind == 'document':
        result = client.study_document(course, job)
    elif kind == 'read':
        result = client.study_read(course, job, info)
    elif kind == 'workid':
        result = client.study_work(course, job, info)
    elif kind == 'empty':
        result = client.study_emptypage(course, unit['point'])
    else:
        raise WorkUnavailable('当前任务点需手动处理：' + str(kind))
    if result.is_failure():
        raise RetryNeeded('任务点处理未成功，稍后先核验平台状态再重试')


def learn_job(payload, reporter):
    descriptor = payload.get('claim_fd')
    if os.name == 'nt':
        from app.point_queue import PointLedger
        current = PointLedger(payload['point_store']).get(payload['unit']['id'])
        if descriptor != -1 or not current or current.get('owner') != payload.get('claim_owner') or current.get('status') != 'running':
            raise WorkUnavailable('任务点没有获得独占执行权，已阻止启动')
    elif descriptor is None:
        raise WorkUnavailable('任务点没有获得独占执行权，已阻止启动')
    else:
        import fcntl
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    unit = payload['unit']
    reporter.update(chapter=unit['point']['title'], unit_id=unit['id'])
    if payload['mode'] == 'demo':
        for second in range(10):
            reporter.checkpoint()
            reporter.update(video_progress=(second+1)*10, video_seconds=second+1, video_total=10)
            time.sleep(float(os.environ.get('DEMO_TICK_SECONDS', '1')))
    else:
        client = platform_client(payload, reporter)
        # Queue metadata may have waited while the slot count was reduced.
        # Re-read this chapter under our exclusive claim before writing progress.
        from app.parallel import read_units
        from app.point_queue import PointLedger
        ledger = PointLedger(payload['point_store'])
        refreshed = None
        for candidate in read_units(client, payload['course'], [unit['point']], reporter, ledger):
            if candidate['id'] == unit['id']:
                refreshed = candidate
                break
            if (ledger.get(unit['id']) or {}).get('status') == 'completed':
                break
        if (ledger.get(unit['id']) or {}).get('status') == 'completed':
            reporter.log('平台已确认任务点不再待完成，跳过执行')
            reporter.update(platform_point_completed=True, status='completed')
            return
        if refreshed is None:
            raise RetryNeeded('当前任务点状态已变化，下次先刷新后再领取')
        unit = refreshed
        execute_job(client, payload['course'], unit, reporter)
    reporter.log('任务点执行结束，等待课程进程核验平台进度')
    reporter.update(status='completed')


def schedule_retry(payload, reporter, reason):
    from app.quiz import DEFAULT_CONFIG
    config = {**DEFAULT_CONFIG, **payload.get("quiz_config", {})}
    count = int(payload.get("previous_state", {}).get("retry_count", 0)) + 1
    reporter.log(reason, "warning")
    if count > config["max_retries"]:
        reporter.update(status="attention", retry_scope='connection', error=f"已用完 {config['max_retries']} 次自动重试：{reason}", retry_count=count - 1,
                        next_retry_at=None, chapter="自动重试已用完，其他课程继续运行")
        return
    delay = min(config["retry_delay"] * 2 ** (count - 1), 900)
    due = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()
    reporter.update(status="retry_wait", retry_scope='connection', error=reason, retry_count=count, next_retry_at=due,
                    chapter=f"第 {count}/{config['max_retries']} 次自动重试 · 等待 {delay} 秒")
    reporter.log("已释放运行位置，其他账号和课程继续执行")


def schedule_point_recovery(reporter, error):
    reporter.log(str(error), 'warning')
    reporter.update(status='retry_wait', retry_scope='points', error=str(error),
                    next_retry_at=datetime.fromtimestamp(error.next_retry, timezone.utc).isoformat(),
                    point_retry_pending=error.pending, point_needs_review=error.blocked,
                    chapter=(f'自动恢复中 · {error.pending} 个任务点待重试 · {error.blocked} 个待处理'
                             if error.pending or error.blocked else '等待平台开放或确认，后台继续检查'))


def main():
    payload = json.loads(sys.stdin.read())
    reporter = Reporter(payload)
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(Cancelled()))
    code = 0
    try:
        reporter.checkpoint()
        if payload.get("action") == "scan":
            result = scan(payload, reporter)
            print(json.dumps({"courses": result}, ensure_ascii=False))
        elif payload.get('action') == 'job':
            learn_job(payload, reporter)
        elif payload.get("action") == "inspect":
            result = inspect_progress(payload, reporter)
            Path("snapshot.json").write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
            reporter.update(status="completed", chapter="任务点状态读取完成")
        else:
            learn(payload, reporter)
    except Cancelled:
        reporter.update(status="cancelled", chapter="任务已停止")
    except PointRecoveryNeeded as error:
        schedule_point_recovery(reporter, error)
        code = 3
    except RetryNeeded as error:
        if payload.get('action') == 'job':
            reporter.log('任务点已交回独立恢复队列：' + str(error), 'warning')
            reporter.update(status='retry_wait', error=str(error), next_retry_at=None)
        else:
            schedule_retry(payload, reporter, str(error))
        code = 3
    except (NeedsAttention, WorkUnavailable) as error:
        reporter.log(str(error), "warning")
        reporter.update(status="attention", error=reporter.clean(error))
        code = 2
    except Exception as error:
        message = f"执行失败（{type(error).__name__}），请检查网络、账号或平台接口后重试"
        reporter.log(message, "error")
        import requests
        if payload.get("action", "learn") in ("learn", "job") and isinstance(error, requests.RequestException):
            schedule_retry(payload, reporter, "网络暂时不可用，稍后自动重试")
            code = 3
        else:
            reporter.update(status="failed", error=message)
            code = 1
    finally:
        reporter.stop.set()
        Path("cookies.txt").unlink(missing_ok=True)
        Path("chaoxing.log").unlink(missing_ok=True)
        if payload.get('claim_fd', -1) >= 0:
            os.close(payload['claim_fd'])
    return code


if __name__ == "__main__":
    sys.exit(main())
