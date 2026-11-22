"""FIFO point scheduling with 1–3 live slots and isolated child processes."""
import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

from app import store
from app.runtime import worker_command, worker_popen_options
from app.point_queue import PointLedger, point_key
from app.retry import RetryNeeded, WorkUnavailable, PointRecoveryNeeded
from datetime import datetime, timezone

PROGRESS_REFRESH_INTERVAL = 30.0


def read_units(client, course, points, reporter, ledger):
    """Read cards without upstream get_job_list's implicit empty-page writes."""
    from api.base import SessionManager
    from api.decode import _extract_job_info, _process_attachment_cards
    from app.progress import flag, is_empty_card_page
    seen = set()
    for point in points:
        reporter.checkpoint()
        if point.get('has_finished') or point.get('need_unlock'):
            continue
        valid, any_jobs, pages, ambiguous = False, False, set(), False
        for page in range(7):
            response = SessionManager.get_session().get('https://mooc1.chaoxing.com/mooc-ans/knowledge/cards', params={
                'clazzid': course['clazzId'], 'courseid': course['courseId'], 'knowledgeid': point['id'],
                'cpi': course['cpi'], 'ut': 's', 'num': str(page), 'mooc2': '1'})
            response.raise_for_status()
            if '章节未开放' in response.text:
                ambiguous = True
                break
            match = re.search(r'\bmArg\s*=\s*(\{.*?\})\s*;', response.text, re.S)
            if not match:
                if is_empty_card_page(response.text):
                    valid = True
                    # The first card may be empty while a later card has work.
                    # Scan all seven before scheduling a reading acknowledgement.
                    continue
                ambiguous = True
                break
            data = json.loads(match[1])
            if 'attachments' not in data:
                ambiguous = True
                break
            signature = json.dumps(data['attachments'], sort_keys=True)
            if signature in pages:
                # Consecutive ordinary text cards both have attachments=[].
                # They do not prove later cards have no required work.
                continue
            pages.add(signature)
            valid = True
            info = _extract_job_info(data)
            for card_index, card in enumerate(data['attachments']):
                # Completed cards retain jobid but lose `job`. Dispatch only
                # pending jobs; disappearance confirms a previously known job.
                if not (flag(card.get('job')) or card.get('jobid') or (card.get('property') or {}).get('_jobid')):
                    continue
                kind = str(card.get('type'))
                required = flag(card.get('job'))
                prop = card.get('property') or {}
                legacy_read = kind == 'read' and prop.get('read') is False
                passed = flag(card.get('isPassed'))
                parsed = {**card, 'isPassed': False, 'jobid': card.get('jobid') or prop.get('_jobid', '')}
                if kind in ('video', 'document', 'workid'):
                    parsed['job'] = True
                elif kind == 'read':
                    parsed.pop('job', None)
                else:
                    if required:
                        job = {'type': kind, 'jobid': parsed['jobid'] or f'unsupported:{point["id"]}:{page}:{card_index}'}
                        key = point_key(point, job)
                        any_jobs = True
                        if key not in seen and (ledger.get(key) or {}).get('status') != 'completed':
                            seen.add(key)
                            yield {'id': key, 'point': point, 'job': job, 'info': info,
                                   'name': prop.get('name') or '未适配任务点：' + kind}
                    continue
                jobs = _process_attachment_cards([parsed])
                if kind == 'read' and not jobs and parsed['jobid']:
                    jobs = [{'type': 'read', 'jobid': parsed['jobid']}]
                for job in jobs:
                    try:
                        key = point_key(point, job)
                    except ValueError as error:
                        raise WorkUnavailable(str(error)) from None
                    known = ledger.get(key)
                    if required or legacy_read or passed or known:
                        any_jobs = True
                    if passed or (known and not required and not legacy_read):
                        ledger.complete(key, point, job)
                        continue
                    if not required and not legacy_read:
                        continue
                    if key in seen or (ledger.get(key) or {}).get('status') == 'completed':
                        continue
                    seen.add(key)
                    yield {'id': key, 'point': point, 'job': job, 'info': info,
                           'name': job.get('name') or (card.get('property') or {}).get('name') or job['type']}
        if valid and not any_jobs and not ambiguous:
            job = {'type': 'empty', 'jobid': 'empty:' + str(point['id'])}
            key = point_key(point, job)
            if (ledger.get(key) or {}).get('status') != 'completed':
                yield {'id': key, 'point': point, 'job': job, 'info': {}, 'name': '章节阅读确认'}


def demo_units(course):
    for index in range(course['done'], course['total']):
        point = {'id': str(index + 1), 'title': f'第 {index + 1} 节 · 演示课程内容'}
        job = {'type': 'video', 'jobid': 'demo-' + str(index + 1)}
        yield {'id': point_key(point, job), 'point': point, 'job': job, 'info': {}, 'name': point['title']}


class PointPool:
    def __init__(self, payload, reporter, ledger, client=None):
        self.payload, self.reporter, self.ledger, self.client = payload, reporter, ledger, client
        self.root = Path.cwd()
        self.active = {}
        self.finished = {}
        self.owner = uuid.uuid4().hex
        self.limit = int(payload.get('point_concurrency', 1))
        self.runtime = Path(payload.get('runtime_path', self.root / 'runtime.json'))
        self.started = 0
        self.peak = int(payload.get('previous_state', {}).get('point_peak', 0))
        self.prior_history = {row['id']: row for row in payload.get('previous_state', {}).get('point_history', [])}
        self.errors = []
        self.log_offsets = {}
        self.completed_attempts = 0

    def slots(self):
        config = store.read_json(self.runtime, {})
        value = config.get('point_concurrency', self.limit)
        if isinstance(value, int) and 1 <= value <= 3:
            self.limit = value
        return self.limit

    def start(self, unit):
        descriptor = self.ledger.claim(unit, self.owner)
        if descriptor is None:
            return False
        self.log_offsets.pop(unit['id'], None)
        self.finished.pop(unit['id'], None)
        self.errors = [entry for entry in self.errors if entry[0] != unit['id']]
        path = self.root / 'units' / unit['id']
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        (path / 'state.json').unlink(missing_ok=True)
        child = {**self.payload, 'action': 'job', 'unit': unit, 'previous_state': {},
                 'parent_pid': os.getpid(), 'control_path': str(self.root / 'control.json'),
                 'claim_fd': descriptor, 'claim_owner': self.owner,
                 'request_gate': str(self.root / 'request-gate.lock')}
        child.pop('task_point_limit', None)
        try:
            if self.payload['mode'] != 'demo':
                child['session_cookies'] = (self.root / 'cookies.txt').read_text()
            child_args = ['--study-task-id=' + str(self.payload.get('task_id', '')),
                          '--study-unit-id=' + str(unit['id'])]
            options = worker_popen_options()
            if os.name != 'nt':
                options['pass_fds'] = (descriptor,)
            process = subprocess.Popen(worker_command(*child_args), cwd=path,
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **options)
            self.active[unit['id']] = {'unit': unit, 'path': path, 'process': process}
            process.stdin.write(json.dumps(child, ensure_ascii=False).encode())
            process.stdin.close()
            self.ledger.update(unit['id'], self.owner, 'running', process.pid)
            self.started += 1
            self.peak = max(self.peak, len(self.active))
            self.reporter.log(f"已领取任务点：{unit['point']['title']} · {unit['name']}（PID {process.pid}）")
            return True
        finally:
            # The child inherited the same locked file description at spawn.
            if os.name != 'nt':
                os.close(descriptor)

    def collect(self):
        from app.worker import NeedsAttention
        for key, item in list(self.active.items()):
            process, path = item['process'], item['path']
            state = store.read_json(path / 'state.json', {})
            offset = self.log_offsets.get(key, 0)
            for line in state.get('logs', [])[offset:]:
                self.reporter.log(item['unit']['name'] + ' · ' + line['message'], line.get('level', 'info'))
            self.log_offsets[key] = len(state.get('logs', []))
            if process.poll() is None:
                continue
            status = state.get('status', 'failed')
            successful = status == 'completed' and process.returncode == 0
            error = state.get('error') or '任务点进程退出，稍后重新核验'
            self.ledger.update(key, self.owner, 'processed' if successful else 'retry', error='' if successful else error)
            if successful and (self.payload['mode'] == 'demo' or state.get('platform_point_completed') is True):
                self.ledger.complete(key)
            self.finished[key] = {**item['unit'], 'status': 'processed' if successful else status, 'error': '' if successful else error,
                                  'pid': process.pid, 'ended_at': state.get('updated_at')}
            del self.active[key]
            self.completed_attempts += 1
            (path / 'cookies.txt').unlink(missing_ok=True)
            if not successful:
                self.errors.append((key, status, error))
                if status == 'attention' and ('验证' in error or '登录' in error):
                    raise NeedsAttention(error)
                config = self.payload.get('quiz_config', {})
                self.ledger.defer(key, self.owner, error, max_failures=config.get('max_retries', 6),
                                  delay=config.get('retry_delay', 60), permanent=status == 'attention')

    def publish(self, done, total):
        rows, pending = [], []
        for key, item in self.active.items():
            state = store.read_json(item['path'] / 'state.json', {})
            rows.append({'id': key, 'name': item['unit']['name'], 'chapter': item['unit']['point']['title'],
                         'status': state.get('status', 'running'), 'pid': item['process'].pid,
                         'video_progress': state.get('video_progress', 0), 'video_seconds': state.get('video_seconds', 0),
                         'video_total': state.get('video_total', 0), 'started_at': state.get('started_at'),
                         'answer_progress': state.get('answer_progress'), 'error': state.get('error')})
            if state.get('pending_quiz'):
                pending.append({**state['pending_quiz'], 'unit_id': key})
        old = (self.reporter.state.get('pending_quiz') or {}).get('id')
        selected = next((quiz for quiz in pending if quiz['id'] == old), pending[0] if pending else None)
        history = dict(self.prior_history)
        for key, unit in self.finished.items():
            history[key] = {'id': key, 'name': unit['name'], 'chapter': unit['point']['title'],
                            'status': unit['status'], 'pid': unit['pid'], 'error': unit['error']}
        for key, row in history.items():
            if (self.ledger.get(key) or {}).get('status') == 'completed':
                row['status'] = 'completed'
                row['error'] = ''
        scope = store.read_json(self.root / 'point-scope.json', None)
        recovering = self.ledger.recoveries(scope)
        for item in recovering:
            item['next_retry_at'] = datetime.fromtimestamp(item['next_retry'], timezone.utc).isoformat()
            item['chapter'] = history.get(item['id'], {}).get('chapter', item['chapter_id'])
            if item['id'] in history and item['id'] not in self.active:
                history[item['id']].update(status='blocked' if item['blocked'] else 'retry_wait',
                    retry_count=item['failures'], next_retry_at=item['next_retry_at'])
        self.reporter.update(status='waiting_answer' if pending else 'running', pending_quiz=selected,
            pending_quiz_count=len(pending), point_concurrency=self.slots(), active_point_count=len(rows), active_points=rows,
            points_processed=len(history), point_peak=self.peak,
            point_history=list(history.values())[-100:],
            point_recovery=recovering[:100], point_retry_pending=sum(not row['blocked'] for row in recovering),
            point_needs_review=sum(bool(row['blocked']) for row in recovering),
            total=total, done=done, progress=round(done / total * 100, 1) if total else 0,
            chapter=f'任务点并发 {self.limit} · 正在执行 {len(rows)} 个 · 平台章节 {done}/{total}')

    def close(self):
        for item in self.active.values():
            if item['process'].poll() is None:
                item['process'].terminate()
        for key, item in self.active.items():
            try:
                item['process'].wait(timeout=3)
            except subprocess.TimeoutExpired:
                item['process'].kill()
                item['process'].wait(timeout=3)
            (item['path'] / 'cookies.txt').unlink(missing_ok=True)
            self.ledger.update(key, self.owner, 'retry', error='本次执行已停止，下次先核验平台状态')
        self.active.clear()


def learn_parallel(payload, reporter):
    from app.worker import platform_client, course_key, NeedsAttention
    demo = payload['mode'] == 'demo'
    course = payload['course']
    if demo:
        if os.environ.get('STUDY_DEMO_RETRY_ONCE') == '1' and not payload.get('previous_state', {}).get('retry_count'):
            raise RetryNeeded('演示：模拟答题接口返回无效 JSON，稍后自动恢复')
        if os.environ.get('STUDY_DEMO_QUIZ') == '1':
            from app.quiz import build_tiku
            build_tiku({'provider': 'manual', 'mode': 'assisted'}, reporter, NeedsAttention).query_all([
                {'id': 'demo-question', 'title': '演示题：1 + 1 等于几？', 'type': 'single', 'options': 'A. 1\nB. 2\nC. 3'}])
        client, total, initial_done = None, course['total'], course['done']
        points = []
        reporter.log('演示任务：任务点使用真实独立子进程；进度为本地模拟，不连接学习通')
    else:
        payload['request_gate'] = str(Path.cwd() / 'request-gate.lock')
        client = platform_client(payload, reporter)
        matches = [c for c in client.get_course_list() if course_key(c) == course_key(course)]
        if not matches:
            raise NeedsAttention('所选课程已不可用，请重新同步课程')
        course = matches[0]
        points = client.get_course_point(course['courseId'], course['clazzId'], course['cpi'])['points']
        if not points:
            raise NeedsAttention('未读取到章节，无法启动并发队列')
        total, initial_done = len(points), sum(bool(p.get('has_finished')) for p in points)
    ledger = PointLedger(payload.get('point_store', Path.cwd() / 'point-state.sqlite'))
    ledger.begin_task(payload.get('task_id'))
    ledger.complete_chapters([p['id'] for p in points if p.get('has_finished')])
    if not demo:
        reporter.update(platform_progress_at=store.now(), platform_progress_error=None)
    pool = PointPool(payload, reporter, ledger, client)
    scope_file = Path('point-scope.json')
    scoped = store.read_json(scope_file, None)
    scan_points = points
    if scoped:
        known = [ledger.get(key) for key in scoped]
        if all(known):
            chapters = {row['chapter_id'] for row in known}
            scan_points = [point for point in points if str(point['id']) in chapters]
    units = iter(demo_units(course) if demo else read_units(client, course, scan_points, reporter, ledger))
    limit = payload.get('task_point_limit')
    queue, exhausted, dispatched, done = [], False, set(), initial_done
    recovery_scans, deferred_attempts = set(), set()
    discovery_error = False
    last_scan_done = initial_done

    def fetch_one():
        nonlocal exhausted, discovery_error
        import requests
        try:
            for unit in units:
                if scoped is not None and unit['id'] not in scoped:
                    continue
                if unit['id'] in dispatched or (ledger.get(unit['id']) or {}).get('status') == 'completed':
                    continue
                if not ledger.ready(unit['id']):
                    continue
                return unit
        except (requests.RequestException, json.JSONDecodeError) as error:
            discovery_error = True
            reporter.log(f'任务卡读取暂时失败（{type(error).__name__}），已在运行的任务继续执行', 'warning')
        exhausted = True
        return None

    try:
        # Freeze a bounded test scope before executing; retries keep these IDs.
        if limit and scoped is None:
            for _ in range(limit):
                unit = fetch_one()
                if unit is None:
                    break
                queue.append(unit)
                dispatched.add(unit['id'])
            if discovery_error:
                raise PointRecoveryNeeded('任务卡读取暂时失败，尚未领取任何验证任务点，稍后自动恢复',
                                          time.time() + payload.get('quiz_config', {}).get('retry_delay', 60))
            scoped = [unit['id'] for unit in queue]
            store.write_json(scope_file, scoped)
            exhausted = True
        last_verified = 0
        last_progress_check = time.monotonic()
        while True:
            reporter.checkpoint()
            pool.collect()
            slots = pool.slots()
            while len(queue) + len(pool.active) < slots and not exhausted:
                unit = fetch_one()
                if unit is not None:
                    queue.append(unit)
                    dispatched.add(unit['id'])
            while queue and len(pool.active) < pool.slots():
                unit = queue.pop(0)
                if not pool.start(unit):
                    # Another living owner holds the point, or it is completed.
                    if (ledger.get(unit['id']) or {}).get('status') != 'completed' and ledger.ready(unit['id']):
                        pool.errors.append((unit['id'], 'retry_wait', '任务点由其他进程持有，下次先核验'))
            if demo:
                done = min(total, initial_done + sum((ledger.get(k) or {}).get('status') == 'completed' for k in pool.finished))
            elif (getattr(pool, 'completed_attempts', len(pool.finished)) != last_verified or
                  time.monotonic() - last_progress_check >= PROGRESS_REFRESH_INTERVAL):
                # A successful request can precede the platform's delayed status
                # update. Continue polling while a long sibling is still alive.
                last_progress_check = time.monotonic()
                last_verified = getattr(pool, 'completed_attempts', len(pool.finished))
                try:
                    verified = client.get_course_point(course['courseId'], course['clazzId'], course['cpi'])['points']
                    if not verified:
                        raise RetryNeeded('平台进度暂不可核验，已保留各任务点状态')
                    done, total = sum(bool(p.get('has_finished')) for p in verified), len(verified)
                    if done > initial_done:
                        payload.setdefault('previous_state', {})['retry_count'] = 0
                        reporter.update(retry_count=0)
                    ledger.complete_chapters([p['id'] for p in verified if p.get('has_finished')])
                    reporter.update(platform_progress_at=store.now(), platform_progress_error=None)
                    changed = {unit['point']['id'] for key, unit in pool.finished.items()
                               if (ledger.get(key) or {}).get('status') != 'completed'}
                    for _ in read_units(client, course, [p for p in verified if p['id'] in changed and not p.get('has_finished')], reporter, ledger):
                        pass
                except Exception as error:
                    if not pool.active:
                        raise
                    # A read-only refresh failure must not terminate playback.
                    reporter.update(platform_progress_error=reporter.clean(str(error)))
                    reporter.log('平台进度刷新暂未成功，保留当前任务继续执行，稍后回读', 'warning')
            pool.publish(done, total)
            if exhausted and not queue and not pool.active:
                if not demo:
                    verified = client.get_course_point(course['courseId'], course['clazzId'], course['cpi'])['points']
                    if not verified:
                        raise RetryNeeded('最终平台进度暂不可核验')
                    done, total = sum(bool(p.get('has_finished')) for p in verified), len(verified)
                    if done > initial_done:
                        payload.setdefault('previous_state', {})['retry_count'] = 0
                        reporter.update(retry_count=0)
                    ledger.complete_chapters([p['id'] for p in verified if p.get('has_finished')])
                    reporter.update(platform_progress_at=store.now(), platform_progress_error=None)
                    pool.publish(done, total)
                    if scoped is None and last_scan_done < done < total:
                        # Sequential courses unlock the next chapter only after
                        # this wave completes. Refresh the queue in the same run,
                        # retaining all claims, completed IDs and error history.
                        last_scan_done = done
                        units = iter(read_units(client, course, verified, reporter, ledger))
                        discovery_error = False
                        exhausted = False
                        reporter.log('平台章节进度已更新，继续扫描新解锁的任务点')
                        continue
                    config = payload.get('quiz_config', {})
                    for key, unit in pool.finished.items():
                        attempt = (key, unit.get('pid'))
                        if (unit.get('status') == 'processed' and attempt not in deferred_attempts
                                and (ledger.get(key) or {}).get('status') != 'completed'):
                            ledger.defer(key, pool.owner, '本次执行已结束，平台任务点尚待确认',
                                max_failures=config.get('max_retries', 6), delay=config.get('retry_delay', 60))
                            deferred_attempts.add(attempt)
                    due = [row for row in ledger.recoveries(scoped) if not row['blocked']
                           and row['next_retry'] <= time.time()
                           and (row['id'], row['failures'], row['next_retry']) not in recovery_scans]
                    if due:
                        for row in due:
                            recovery_scans.add((row['id'], row['failures'], row['next_retry']))
                        dispatched.difference_update(row['id'] for row in due)
                        units = iter(read_units(client, course, verified, reporter, ledger))
                        discovery_error = False
                        exhausted = False
                        reporter.log(f'继续恢复已到期的 {len(due)} 个任务点，已完成任务点保持跳过')
                        continue
                break
            time.sleep(0.15)
        pool.publish(done, total)
        errors = [(status, error) for key, status, error in pool.errors
                  if (ledger.get(key) or {}).get('status') != 'completed']
        recovering = ledger.recoveries(scoped)
        def await_points(reason):
            pending = [row for row in recovering if not row['blocked']]
            blocked = len(recovering) - len(pending)
            delay = payload.get('quiz_config', {}).get('retry_delay', 60)
            next_retry = max(time.time() + delay, min(row['next_retry'] for row in pending)) if pending else time.time() + (900 if blocked else delay)
            raise PointRecoveryNeeded(reason, next_retry, len(pending), blocked)
        if errors or recovering or (discovery_error and done < total):
            reason = f'任务点独立恢复：{sum(not r["blocked"] for r in recovering)} 个待重试，{sum(bool(r["blocked"]) for r in recovering)} 个待处理'
            if errors:
                reason += '；' + errors[0][1]
            if discovery_error:
                reason += '；任务卡读取暂时失败'
            await_points(reason)
        if scoped is not None:
            confirmed = sum((ledger.get(key) or {}).get('status') == 'completed' for key in scoped)
            if confirmed < len(scoped):
                await_points(f'本次范围平台已确认 {confirmed}/{len(scoped)} 个任务点，稍后核验')
            reporter.update(status='completed', partial_scope=True, scope_completed=confirmed,
                task_point_limit=limit, chapter=f'本次 {confirmed} 个任务点已确认完成；课程章节 {done}/{total}')
        elif done == total:
            reporter.update(status='completed', progress=100, chapter='平台已确认课程所有章节完成')
        else:
            await_points(f'平台确认 {done}/{total} 节完成，等待平台开放或任务点确认；后台自动检查')
    finally:
        pool.close()
        reporter.update(active_point_count=0, active_points=[])
