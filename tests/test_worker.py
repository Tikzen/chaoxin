from unittest.mock import Mock

import pytest

from app import worker
from app.retry import RetryNeeded


@pytest.fixture
def engine(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    course = {"courseId": "123", "clazzId": "class-A", "cpi": "cpi-A", "title": "测试课程"}
    payload = {"mode": "real", "course": course, "credentials": {"username": "user", "password": "secret"}}
    client = Mock()
    client.get_course_list.return_value = [course]
    monkeypatch.setattr(worker, "platform_client", lambda *_: client)
    return payload, client, worker.Reporter(payload)


def test_exact_class_selection_never_falls_back_to_all(engine):
    payload, client, reporter = engine
    client.get_course_list.return_value = [{**payload["course"], "clazzId": "different-class"}]
    with pytest.raises(worker.NeedsAttention, match="不可用"):
        worker.learn(payload, reporter)
    client.get_course_point.assert_not_called()


def test_empty_chapters_require_attention(engine):
    payload, client, reporter = engine
    client.get_course_point.return_value = {"points": []}
    with pytest.raises(worker.NeedsAttention, match="未读取到章节"):
        worker.learn(payload, reporter)
    assert reporter.state["status"] != "completed"


def test_quiz_is_executed_and_requires_platform_confirmation(engine):
    payload, client, reporter = engine
    client.get_course_point.return_value = {"points": [{"id": "1", "title": "章节测验", "has_finished": False}]}
    client.get_job_list.return_value = ([{"type": "workid", "name": "测验"}], {})
    client.study_work.return_value.is_failure.return_value = False
    with pytest.raises(RetryNeeded, match="尚未完成"):
        worker.learn(payload, reporter)
    client.study_work.assert_called_once()
    assert reporter.state["progress"] == 0


def test_successful_http_result_still_requires_platform_confirmation(engine):
    payload, client, reporter = engine
    client.get_course_point.return_value = {"points": [{"id": "1", "title": "文档", "has_finished": False}]}
    client.get_job_list.return_value = ([{"type": "document", "name": "资料"}], {})
    client.study_document.return_value.is_failure.return_value = False
    with pytest.raises(RetryNeeded, match="平台确认 0/1"):
        worker.learn(payload, reporter)
    assert reporter.state["progress"] == 0


def test_verified_completion(engine):
    payload, client, reporter = engine
    point = {"id": "1", "title": "阅读", "has_finished": False}
    client.get_course_point.side_effect = [{"points": [point]}, {"points": [{**point, "has_finished": True}]},
                                           {"points": [{**point, "has_finished": True}]}]
    client.get_job_list.return_value = ([{"type": "read", "name": "文章"}], {})
    client.study_read.return_value.is_failure.return_value = False
    worker.learn(payload, reporter)
    assert reporter.state["status"] == "completed"
    assert reporter.state["progress"] == 100


def test_ci_environment_does_not_enable_cloud_reporter(engine, monkeypatch):
    payload, _, _ = engine
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.delenv("STUDY_REMOTE_WORKER", raising=False)
    assert worker.Reporter(payload).remote is None


def test_retry_connection_failure_preserves_last_verified_progress(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    reporter = worker.Reporter({'previous_state': {'done': 95, 'total': 111, 'progress': 85.6,
                                                  'platform_progress_at': '2026-11-15T12:30:00+00:00'}})
    assert reporter.state['done'] == 95 and reporter.state['total'] == 111
    assert reporter.state['progress'] == 85.6
    assert reporter.state['platform_progress_at'] == '2026-11-15T12:30:00+00:00'


def test_submit_http_guard_keeps_raw_response_before_authentication_error(tmp_path, monkeypatch):
    import json,requests
    monkeypatch.chdir(tmp_path)
    result = Mock(status_code=403, text='denied by platform', url='https://mooc1.chaoxing.com/mooc-ans/work/addStudentWorkNewWeb')
    result.headers = {'Content-Type': 'text/html'}
    monkeypatch.setattr(requests.Session, 'request', lambda *args, **kwargs: result)
    payload = {'action': 'inspect', 'credentials': {'username': 'test-user', 'password': 'test-password'}, 'session_cookies': 'test=1'}
    reporter = worker.Reporter(payload)
    worker.platform_client(payload, reporter)
    with pytest.raises(worker.NeedsAttention):
        requests.Session().post(result.url)
    evidence = json.loads((tmp_path / 'submit-response.json').read_text())
    assert evidence['http_status'] == 403 and evidence['response_body'] == 'denied by platform'
