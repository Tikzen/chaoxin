import json
import sys
from unittest.mock import Mock

import pytest

from app import quiz, store, worker


@pytest.mark.parametrize("kind,value,expected", [
    ("single", "B", "B. 2"), ("multiple", "AC", "A. 1\nC. 3"),
    ("judgement", "false", "错误"), ("completion", "答案", "答案")])
def test_answers_are_normalized(kind, value, expected):
    assert quiz.normalize_answer({"type": kind, "options": "A. 1\nB. 2\nC. 3"}, value) == expected


@pytest.mark.parametrize("kind,value", [("single", ""), ("single", "AC"), ("multiple", "Z"), ("judgement", "maybe"), ("unknown", "test")])
def test_incomplete_or_unmatched_answers_are_rejected(kind, value):
    with pytest.raises(ValueError):
        quiz.normalize_answer({"type": kind, "options": "A. 1\nB. 2\nC. 3"}, value)


def test_upstream_quiz_flow_waits_then_submits_without_random_answers(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(store.ROOT / "vendor" / "chaoxing"))
    from api import base
    from api.logger import logger
    logger.remove()
    reporter = worker.Reporter({})
    original_update = reporter.update
    def update(**values):
        original_update(**values)
        if values.get("status") == "waiting_answer":
            pending = values["pending_quiz"]
            (tmp_path / f"answers-{pending['id']}.json").write_text(json.dumps({"answers": {"q1": "B"}}))
    reporter.update = update
    provider = quiz.build_tiku({"provider": "manual", "mode": "assisted"}, reporter, worker.NeedsAttention)
    client = base.Chaoxing(tiku=provider, work_redo_enabled=False)
    session = Mock()
    session.get.return_value.status_code = 200
    session.get.return_value.text = "question HTML"
    session.post.return_value.status_code = 200
    session.post.return_value.json.return_value = {"status": True, "msg": "ok"}
    monkeypatch.setattr(base.SessionManager, "get_session", lambda: session)
    questions = {"workId": "w1", "questions": [{"id": "q1", "title": "1+1?", "type": "single", "options": "A. 1\nB. 2",
                 "answerField": {"answerq1": "", "answertypeq1": "0"}}]}
    monkeypatch.setattr(base, "decode_questions_info", lambda _: questions)
    monkeypatch.setattr(client, "_check_work_result", lambda *_: {"all_correct": True, "score": 100, "unknown": 0})
    result = client.study_work({"courseId": "1", "clazzId": "2"}, {"jobid": "work-1", "enc": "e"},
                               {"knowledgeid": "k", "ktoken": "t", "cpi": "c"})
    assert result.is_success()
    submitted = session.post.call_args.kwargs["data"]
    assert submitted["answerq1"] == "B"
    assert submitted["pyFlag"] == ""  # Submit, not save-only.
    assert reporter.state["pending_quiz"] is None
    with pytest.raises(worker.NeedsAttention, match="随机"):
        base.random_answer("A. 1", "single")
