import json
import sqlite3
import time
from unittest.mock import Mock

from app import store
from app.progress import inspect_course, parse_cards


def test_card_parser_keeps_completed_and_quiz_tasks_and_deduplicates():
    data = {"attachments": [
        {"job": True, "jobid": "v1", "type": "video", "isPassed": True, "property": {"name": "视频"}},
        {"job": True, "jobid": "w1", "type": "workid", "isPassed": False, "property": {"name": "测验"}},
        {"job": False, "type": "document", "property": {"name": "非任务资料"}}]}
    tasks, valid = parse_cards("mArg = " + json.dumps(data) + ";", {})
    assert valid
    assert len(tasks) == 2
    assert tasks[0]["status"] == "completed"
    assert tasks[1]["requires_answer"]
    assert parse_cards("<html>登录失效</html>", {}) == ([], False)


def test_progress_scan_reads_only_and_deduplicates_cards():
    client, session, reporter = Mock(), Mock(), Mock()
    client.get_course_point.return_value = {"points": [{"id": "c1", "title": "第一节", "has_finished": False, "jobCount": 2}]}
    html = "mArg=" + json.dumps({"attachments": [
        {"job": True, "jobid": "v1", "type": "video", "isPassed": True},
        {"job": True, "jobid": "w1", "type": "workid", "isPassed": False}]}) + ";"
    session.get.return_value.text = html
    result = inspect_course(client, {"courseId": "1", "clazzId": "2", "cpi": "3"}, session, reporter)
    assert result["task_total"] == 2  # Same attachments returned in seven pages.
    assert result["task_done"] == 1
    assert result["done"] == 0
    assert result["tasks_complete"]
    client.get_job_list.assert_not_called()
    client.study_emptypage.assert_not_called()
    client.study_work.assert_not_called()
    session.post.assert_not_called()


def test_locked_and_missing_metadata_are_not_reported_as_zero_tasks():
    client, session = Mock(), Mock()
    client.get_course_point.return_value = {"points": [{"id": "c1", "title": "未开放", "need_unlock": True}]}
    result = inspect_course(client, {"courseId": "1", "clazzId": "2", "cpi": "3"}, session, Mock())
    assert not result["tasks_complete"]
    assert not result["chapters"][0]["tasks_loaded"]
    session.get.assert_not_called()


def test_old_database_is_migrated_without_losing_courses(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DATA", tmp_path)
    with sqlite3.connect(tmp_path / "study.db") as conn:
        conn.execute("CREATE TABLE courses (id TEXT PRIMARY KEY,account_id TEXT,title TEXT,teacher TEXT,raw TEXT,total INTEGER,done INTEGER,synced_at TEXT)")
        conn.execute("INSERT INTO courses VALUES ('legacy','a','旧课程','老师','{}',10,4,'2026-11-01')")
    store.initialize()
    course = store.one("SELECT * FROM courses WHERE id='legacy'")
    assert course["total"] == 10 and course["done"] == 4
    assert course["source_position"] == 1
    assert course["progress_status"] == "idle"


def test_verified_point_ledger_updates_task_progress_without_finishing_whole_chapter():
    from app.point_queue import point_key
    chapter={'id':'p','has_finished':False}
    data={'attachments':[{'jobid':'v1','type':'video','property':{'name':'done video'}},
                         {'jobid':'image-decoration','type':'image','property':{'name':'not a point'}}]}
    key=point_key(chapter,{'type':'video','jobid':'v1'})
    tasks,valid=parse_cards('mArg='+json.dumps(data)+';',chapter,{key})
    assert valid and len(tasks)==1
    assert tasks[0]['status']=='completed'
    assert chapter['has_finished'] is False


EMPTY_CARD = '''<html><script>mArg = "";</script>
<div id="pageDiv" class="ans-cc"><div class="nullpage"><dl>
<dt><img src="/images/nullImg.png"></dt><dd>暂无内容</dd></dl></div></div></html>'''


def test_explicit_empty_card_is_known_zero_tasks_but_not_a_completed_chapter():
    from app.progress import is_empty_card_page
    assert is_empty_card_page(EMPTY_CARD)
    assert parse_cards(EMPTY_CARD, {'id': 'empty'}) == ([], True)
    assert not is_empty_card_page('<html>暂无内容，请重新登录</html>')
    assert not is_empty_card_page(EMPTY_CARD.replace('</dl>', '</dl><iframe src="/work"></iframe>'))
    assert not is_empty_card_page(EMPTY_CARD.replace('暂无内容', '登录已失效'))
    assert not is_empty_card_page(EMPTY_CARD.replace('mArg = "";', 'mArg = null;'))
    client, session = Mock(), Mock()
    # Upstream defaults jobCount to 1 even when the page has no task counter.
    client.get_course_point.return_value = {'points': [{'id': 'empty', 'title': '空章节', 'jobCount': 1, 'has_finished': False}]}
    session.get.return_value.text = EMPTY_CARD
    snapshot = inspect_course(client, {'courseId': '1', 'clazzId': '2', 'cpi': '3'}, session, Mock())
    assert session.get.call_count == 7
    assert snapshot['tasks_complete'] and snapshot['task_total'] == 0
    assert snapshot['chapters'][0]['empty'] and snapshot['chapters'][0]['tasks_loaded']
    assert snapshot['done'] == 0 and not snapshot['chapters'][0]['completed']
    client.study_emptypage.assert_not_called()
    session.post.assert_not_called()
