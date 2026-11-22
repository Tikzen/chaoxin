import json
import os
import shutil
import subprocess
import sys
import time
from collections import Counter
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests

from app import store, quiz, worker, parallel, materials
from app.point_queue import PointLedger, point_key
from app.retry import PointRecoveryNeeded, RetryNeeded


def unit(key='one'):
    point = {'id': key, 'title': key}
    job = {'type': 'workid', 'jobid': key}
    return {'id': point_key(point, job), 'point': point, 'job': job, 'name': key, 'info': {}}


def test_durable_point_cooldown_budget_completion_and_new_manual_task(tmp_path, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr('app.point_queue.time.time', lambda: clock[0])
    ledger = PointLedger(tmp_path / 'points.sqlite')
    u = unit(); key = u['id']
    ledger.begin_task('first-task')
    for failure in (1, 2, 3):
        fd = ledger.claim(u, str(failure)); assert fd is not None
        ledger.defer(key, str(failure), 'temporary', max_failures=3, delay=10)
        os.close(fd)
        entry = ledger.recoveries()[0]
        assert entry['failures'] == failure
        assert not PointLedger(ledger.path).ready(key)
        assert ledger.claim(u, 'too-soon') is None
        ledger.begin_task('first-task')  # Automatic process restart preserves budget.
        assert ledger.recoveries()[0]['failures'] == failure
        clock[0] = entry['next_retry'] + 1
    assert ledger.recoveries()[0]['blocked'] and not ledger.ready(key)
    ledger.begin_task('manual-new-task')
    assert ledger.ready(key) and ledger.recoveries() == []
    ledger.complete(key, u['point'], u['job'])
    ledger.reset_recoveries()
    assert ledger.claim(u, 'never-repeat-completed') is None
    with ledger.db() as db:
        assert len(db.execute('PRAGMA table_info(points)').fetchall()) == 9


def test_stale_owner_cannot_consume_another_points_retry_budget(tmp_path):
    ledger = PointLedger(tmp_path / 'points.sqlite'); u = unit()
    fd = ledger.claim(u, 'new-owner')
    ledger.defer(u['id'], 'old-owner', 'stale failure')
    assert ledger.recoveries() == []
    os.close(fd)


def test_point_recovery_does_not_exhaust_course_budget(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    reporter = worker.Reporter({'previous_state': {'retry_count': 6, 'done': 112, 'total': 148}})
    worker.schedule_point_recovery(reporter, PointRecoveryNeeded('one point cooling down', 2000000000, 1, 2))
    assert reporter.state['status'] == 'retry_wait' and reporter.state['retry_scope'] == 'points'
    assert reporter.state['retry_count'] == 6 and reporter.state['done'] == 112
    assert reporter.state['point_retry_pending'] == 1 and reporter.state['point_needs_review'] == 2


def test_partial_validated_answers_survive_a_new_worker_and_changed_options_invalidate(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    payload = {'point_store': str(tmp_path / 'points.sqlite')}
    questions = [{'id': name, 'title': name, 'type': 'single', 'options': 'A. one\nB. two'} for name in ('good', 'bad')]
    calls = []
    def query(q):
        calls.append(q['id'])
        return 'A' if q['id'] == 'good' else None
    bank = Mock(); bank._query.side_effect = query
    monkeypatch.setattr(quiz, 'make_source', lambda *args: bank)
    with pytest.raises(RetryNeeded):
        quiz.build_tiku({}, worker.Reporter(payload), worker.NeedsAttention).resolve(questions)
    assert calls == ['good', 'bad']
    bank._query.side_effect = lambda q: calls.append(q['id']) or 'B'
    assert quiz.build_tiku({}, worker.Reporter(payload), worker.NeedsAttention).resolve(questions) == ['A', 'B']
    assert calls == ['good', 'bad', 'bad']
    questions[0]['options'] = 'A. changed\nB. two'
    assert quiz.build_tiku({}, worker.Reporter(payload), worker.NeedsAttention).resolve(questions) == ['B', 'B']
    assert calls[-1] == 'good' and calls.count('good') == 2


def test_truncated_question_increases_budget_across_workers_without_requerying_good_answers(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    payload = {'point_store': str(tmp_path / 'points.sqlite')}
    config = {'provider': 'AI', 'fallbacks': [], 'endpoint': 'http://localhost/v1', 'model': 'test', 'attempts': 1}
    questions = [{'id': name, 'title': name, 'type': 'single', 'options': 'A. one\nB. two'} for name in ('good', 'hard')]
    calls = []
    def query(name, question, cfg, repair=False):
        calls.append((question['id'], cfg['_ai_token_budget'], repair))
        if question['id'] == 'hard' and len(calls) < 6:
            raise quiz.AnswerTruncated('output truncated')
        return 'A'
    monkeypatch.setattr(quiz, 'query_ai', query)
    for _ in range(4):
        with pytest.raises(RetryNeeded):
            quiz.build_tiku(config, worker.Reporter(payload), worker.NeedsAttention).resolve(questions)
    assert quiz.build_tiku(config, worker.Reporter(payload), worker.NeedsAttention).resolve(questions) == ['A', 'A']
    assert calls == [('good', 8192, False), ('hard', 8192, False), ('hard', 16384, True),
                     ('hard', 32768, True), ('hard', 32768, True), ('hard', 32768, True)]
    from app.answer_cache import AnswerCache
    assert AnswerCache(payload['point_store'], {**quiz.DEFAULT_CONFIG, **config}).hint(questions[1]) == (0, '')


def test_ai_budget_timeout_and_truncation_are_explicit(monkeypatch):
    response = Mock()
    response.json.return_value = {'choices': [{'finish_reason': 'length', 'message': {'content': ''}}]}
    post = Mock(return_value=response)
    monkeypatch.setattr(requests, 'post', post)
    with pytest.raises(quiz.AnswerTruncated, match='8192'):
        quiz.query_ai('AI', {'title': 'test', 'type': 'judgement'}, {**quiz.DEFAULT_CONFIG, 'endpoint': 'http://localhost/v1'})
    assert post.call_args.kwargs['json']['max_tokens'] == 8192
    assert post.call_args.kwargs['timeout'][1] >= 90
    response.json.return_value = {'choices': [{'finish_reason': 'stop', 'message': {'content': '{"Answer":true}'}}]}
    assert quiz.query_ai('AI', {'title': 'test', 'type': 'judgement'},
                         {**quiz.DEFAULT_CONFIG, 'endpoint': 'http://localhost/v1', '_ai_token_budget': 999999}) is True
    assert post.call_args.kwargs['json']['max_tokens'] == 32768
    assert post.call_args.kwargs['timeout'][1] == 180


@pytest.mark.skipif(not shutil.which('lsof'), reason='process adoption requires native lsof')
def test_controller_reattaches_real_surviving_worker_without_relaunch_or_budget_consumption(tmp_path, monkeypatch):
    from app import manager as management
    from app.process_recovery import AttachedWorker
    root = tmp_path / 'app-root'; script = root / 'app/worker.py'
    script.parent.mkdir(parents=True)
    script.write_text('import time\nfrom pathlib import Path\nPath("ready").touch()\ntime.sleep(60)\n')
    data = tmp_path / 'private'; path = data / 'jobs/task'; path.mkdir(parents=True)
    monkeypatch.setattr(store, 'ROOT', root)
    monkeypatch.setattr(store, 'DATA', data)
    process = subprocess.Popen([sys.executable, '-X', 'utf8', str(script)], cwd=path, start_new_session=True)
    controller = None
    try:
        deadline = time.monotonic() + 5
        while not (path / 'ready').exists():
            assert time.monotonic() < deadline
            time.sleep(.02)
        assert AttachedWorker.attach(process.pid, tmp_path) is None
        state = {'status': 'running', 'pid': process.pid, 'progress': 70, 'done': 7, 'total': 10, 'retry_count': 3}
        store.write_json(path / 'state.json', state)
        monkeypatch.setattr(store, 'execute', Mock())
        monkeypatch.setattr(store, 'rows', lambda *a: [{'id': 'task', 'status': 'running', 'state': json.dumps({'status': 'running', 'progress': 20})}])
        updated = Mock(); monkeypatch.setattr(management, 'update_task', updated)
        monkeypatch.setattr(management.Manager, 'loop', lambda self: self.stop.wait(30))
        controller = management.Manager(); controller.start()
        attached = controller.processes['task']
        assert attached.pid == process.pid and attached.poll() is None
        restored = updated.call_args.args[1]
        assert restored['status'] == 'running' and restored['progress'] == 70 and restored['retry_count'] == 3
        assert len(controller.processes) == 1
        # An inconclusive process probe must not enqueue a second root worker.
        with monkeypatch.context() as uncertain:
            from app.process_recovery import InspectionUnavailable
            uncertain.setattr(AttachedWorker, 'attach', Mock(side_effect=InspectionUnavailable()))
            guarded = management.Manager()
            try:
                guarded.start()
                assert not guarded.processes
                assert updated.call_args.args[1]['status'] == 'attention'
            finally:
                guarded.stop.set(); guarded.thread.join(timeout=3)
        with monkeypatch.context() as unavailable:
            from app.process_recovery import InspectionUnavailable
            unavailable.setattr('app.process_recovery.identity', Mock(side_effect=InspectionUnavailable()))
            signal = Mock(); unavailable.setattr('app.process_recovery.os.kill', signal)
            assert attached.poll() is None and not attached.safe_group()
            attached.terminate(); signal.assert_not_called()
        # Never signal a different process after a PID is reused.
        with monkeypatch.context() as changed:
            changed.setattr('app.process_recovery.identity', lambda pid: (('different',), 'different process'))
            signal = Mock(); changed.setattr('app.process_recovery.os.kill', signal)
            attached.terminate()
            assert attached.poll() == 0 and not attached.safe_group()
            signal.assert_not_called()
    finally:
        if controller:
            controller.stop.set(); controller.thread.join(timeout=3)
        process.terminate(); process.wait(timeout=5)


@pytest.mark.parametrize('failure', [403, 429, 'tls_eof'])
def test_temporary_material_failures_are_recoverable(failure):
    session = Mock()
    if failure == 'tls_eof':
        session.get.side_effect = requests.exceptions.SSLError('SSLEOFError: EOF occurred in violation of protocol')
    else:
        session.get.return_value.status_code = failure
    with pytest.raises(RetryNeeded):
        materials.fetch_material(session, 'https://p.ananas.chaoxing.com/image.png', 'https://mooc1.chaoxing.com/work')


@pytest.mark.parametrize('concurrency', [1, 2, 3])
@pytest.mark.parametrize('permanent', [False, True])
def test_failure_isolated_from_healthy_point_and_due_point_retries_without_replaying_healthy(tmp_path, monkeypatch, permanent, concurrency):
    monkeypatch.chdir(tmp_path)
    (tmp_path / 'cookies.txt').write_text('test=1')
    course = {'courseId': '1', 'clazzId': '2', 'cpi': '3'}
    counts, passed = Counter(), set()
    units = [unit('bad'), unit('healthy')]
    def chapters(*args):
        return {'points': [{**u['point'], 'has_finished': u['point']['id'] in passed} for u in units]}
    client = Mock(); client.get_course_list.return_value = [course]; client.get_course_point.side_effect = chapters
    monkeypatch.setattr(worker, 'platform_client', lambda *args: client)
    def discovery(client, course, points, reporter, ledger):
        for point in points:
            if not point.get('has_finished'):
                yield next(u for u in units if u['point']['id'] == point['id'])
    monkeypatch.setattr(parallel, 'read_units', discovery)
    class Process:
        next_pid = 100
        def __init__(self, *args, cwd, **kwargs):
            Process.next_pid += 1; self.pid = Process.next_pid; self.path = cwd; self.returncode = None
            self.stdin = SimpleNamespace(write=self.receive, close=lambda: None)
        def receive(self, raw):
            payload = json.loads(raw); key = payload['unit']['point']['id']; counts[key] += 1
            failed = key == 'bad' and (permanent or counts[key] == 1)
            state = {'status': ('attention' if permanent else 'retry_wait') if failed else 'completed',
                     'error': ('unsupported material' if permanent else 'temporary') if failed else None, 'logs': []}
            if not failed:passed.add(key);state['platform_point_completed'] = True
            self.returncode = 3 if failed else 0
            store.write_json(self.path / 'state.json', state)
        def poll(self): return self.returncode
    monkeypatch.setattr(parallel.subprocess, 'Popen', Process)
    reporter = worker.Reporter({})
    payload = {'mode': 'real', 'course': course, 'point_store': str(tmp_path / 'points.sqlite'),
               'point_concurrency': concurrency, 'quiz_config': {'max_retries': 3, 'retry_delay': 0}}
    if permanent:
        with pytest.raises(PointRecoveryNeeded) as caught:
            parallel.learn_parallel(payload, reporter)
        assert caught.value.blocked == 1 and caught.value.pending == 0
        assert counts == {'bad': 1, 'healthy': 1}
    else:
        parallel.learn_parallel(payload, reporter)
        assert reporter.state['status'] == 'completed' and reporter.state['done'] == 2
        assert counts == {'bad': 2, 'healthy': 1}
        assert len(reporter.state['point_history']) == 2
