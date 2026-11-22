import json
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from app import main as server
from app import store
from app.manager import Manager, update_task


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DATA", tmp_path)
    monkeypatch.setattr(server, "manager", Manager())
    monkeypatch.setenv("DEMO_TICK_SECONDS", "0.03")
    with TestClient(server.app) as client:
        assert client.post("/api/auth", json={"token": store.token()}).status_code == 200
        yield client


def demo(client):
    account_id = client.post("/api/demo").json()["account_id"]
    courses = client.get("/api/dashboard").json()["courses"]
    return account_id, next(c for c in courses if c["done"] < c["total"])


def task(client, task_id):
    return next(t for t in client.get("/api/dashboard").json()["tasks"] if t["id"] == task_id)


def wait_for(client, task_id, statuses, timeout=25):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = task(client, task_id)
        if value["status"] in statuses:
            return value
        time.sleep(0.05)
    pytest.fail(f"Task did not reach {statuses}: {value}")


def test_auth_csrf_and_static(client):
    client.cookies.clear()
    assert client.get("/api/dashboard").status_code == 401
    assert client.get("/").status_code == 200
    assert client.get("/api/health").json() == {"status": "ok"}
    assert client.post("/api/auth", json={"token": "wrong"}).status_code == 401
    assert client.post("/api/auth", json={"token": "错误口令"}).status_code == 401
    assert client.post("/api/auth", json={"token": store.token()}, headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.post("/api/auth", json={"token": store.token()}).status_code == 200
    response = client.get("/api/dashboard")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-frame-options"] == "DENY"


def test_account_encryption_duplicate_and_password_change(client):
    body = {"name": "测试账号", "username": "13800138000", "password": "do-not-leak-test-secret"}
    response = client.post("/api/accounts", json=body)
    assert response.status_code == 201
    account_id = response.json()["id"]
    assert body["password"] not in response.text
    assert body["username"] not in response.text
    assert client.post("/api/accounts", json=body).status_code == 409
    record = store.one("SELECT * FROM accounts WHERE id=?", (account_id,))
    assert body["password"] not in record["credentials"]
    assert store.unseal(record["credentials"])["password"] == body["password"]
    assert client.patch(f"/api/accounts/{account_id}", json={"password": "new-test-secret"}).status_code == 200
    assert "new-test-secret" not in client.get("/api/dashboard").text
    assert client.delete(f"/api/accounts/{account_id}").status_code == 200


def test_demo_idempotent_sync_and_task_finishes(client):
    account_id, course = demo(client)
    assert client.post("/api/demo").json()["account_id"] == account_id
    assert client.post(f"/api/accounts/{account_id}/sync").json()["count"] == 4
    assert len(client.get("/api/dashboard").json()["courses"]) == 4
    response = client.post("/api/tasks", json={"course_ids": [course["id"]], "engine": "server"})
    assert response.status_code == 201
    task_id = response.json()["task_ids"][0]
    result = wait_for(client, task_id, ["completed"])
    assert result["progress"] == 100
    assert result["state"]["logs"]
    assert any("演示" in l["message"] for l in result["state"]["logs"])
    assert not (store.DATA / "jobs" / task_id / "cookies.txt").exists()


def test_task_pause_resume_cancel_and_duplicate(client, monkeypatch):
    monkeypatch.setenv("DEMO_TICK_SECONDS", "0.15")
    account_id, course = demo(client)
    task_id = client.post("/api/tasks", json={"course_ids": [course["id"]]}).json()["task_ids"][0]
    assert client.post("/api/tasks", json={"course_ids": [course["id"]]}).status_code == 409
    assert client.delete(f"/api/accounts/{account_id}").status_code == 409
    wait_for(client, task_id, ["running"])
    assert client.post(f"/api/tasks/{task_id}/control", json={"action": "pause"}).status_code == 200
    assert task(client, task_id)["status"] == "paused"
    assert client.post(f"/api/tasks/{task_id}/control", json={"action": "run"}).status_code == 200
    assert client.post(f"/api/tasks/{task_id}/control", json={"action": "cancel"}).status_code == 200
    assert wait_for(client, task_id, ["cancelled"])["status"] == "cancelled"
    assert client.delete(f"/api/accounts/{account_id}").status_code == 200


def test_atomic_batch_validation(client):
    _, course = demo(client)
    assert client.post("/api/tasks", json={"course_ids": [course["id"], "missing"]}).status_code == 404
    assert not client.get("/api/dashboard").json()["tasks"]
    assert client.post("/api/tasks", json={"course_ids": [course["id"]], "engine": "bad"}).status_code == 400
    assert client.post("/api/tasks", json={"course_ids": [course["id"]], "engine": "github"}).status_code == 400
    assert client.put("/api/settings", json={"concurrency": 0}).status_code == 422
    assert client.put("/api/settings", json={"concurrency": 2}).status_code == 200


def test_local_mode_rejects_cloud_execution(client):
    _, course = demo(client)
    store.set_setting("github", {"token": "legacy-token"})
    response = client.post("/api/tasks", json={"course_ids": [course["id"]], "engine": "github"})
    assert response.status_code == 400
    assert client.put("/api/github", json={}).status_code == 404
    dashboard = client.get("/api/dashboard").json()
    assert dashboard["execution_mode"] == "local"
    assert "github" not in dashboard
    assert "legacy-token" not in str(dashboard)


@pytest.mark.parametrize("has_state", [False, True])
def test_force_termination_is_cancelled_even_during_startup(client, has_state):
    _, course = demo(client)
    # Simulate a Windows TerminateProcess result: no Python signal/finally handler runs.
    server.manager.stop.set()
    server.manager.thread.join(timeout=3)
    task_id = client.post("/api/tasks", json={"course_ids": [course["id"]]}).json()["task_ids"][0]
    update_task(task_id, {"status": "cancelling", "progress": 5, "logs": []})
    path = store.DATA / "jobs" / task_id
    path.mkdir(exist_ok=True)
    if has_state:
        store.write_json(path / "state.json", {"status": "running", "progress": 5, "logs": []})
    process = Mock()
    process.poll.return_value = 1
    server.manager.processes[task_id] = process
    server.manager.tick()
    assert task(client, task_id)["status"] == "cancelled"


def test_restart_schedules_unattended_local_task_recovery(client):
    _, course = demo(client)
    server.manager.stop.set()
    server.manager.thread.join(timeout=3)
    task_id = client.post("/api/tasks", json={"course_ids": [course["id"]]}).json()["task_ids"][0]
    update_task(task_id, {"status": "running", "progress": 25, "logs": []})
    restarted = Manager()
    restarted.start()
    restarted.shutdown()
    assert task(client, task_id)["status"] == "retry_wait"
    assert task(client, task_id)["progress"] == 25


def test_worker_failure_never_exposes_credentials(tmp_path):
    payload = {"mode": "demo", "credentials": {"password": "never-leak-me"}, "course": {"total": 0, "done": 0}}
    result = subprocess.run([sys.executable, str(store.ROOT / "app" / "worker.py")], cwd=tmp_path,
                            input=json.dumps(payload), text=True, capture_output=True, timeout=10)
    assert result.returncode == 1
    output = (tmp_path / "state.json").read_text(encoding="utf-8")
    assert "never-leak-me" not in output + result.stdout + result.stderr
    assert json.loads(output)["status"] == "failed"


def test_duplicate_server_is_rejected(client):
    with pytest.raises(RuntimeError, match="已有控制台运行"):
        with store.runtime_lock():
            pytest.fail("Second server must not acquire the lock")


def test_switching_accounts_returns_only_their_own_courses(client):
    first, _ = demo(client)
    second = client.post("/api/demo?new_account=true").json()["account_id"]
    assert first != second
    a = client.get(f"/api/accounts/{first}/courses").json()
    b = client.get(f"/api/accounts/{second}/courses").json()
    assert len(a["courses"]) == len(b["courses"]) == 4
    assert {c["account_id"] for c in a["courses"]} == {first}
    assert {c["account_id"] for c in b["courses"]} == {second}
    assert not ({c["id"] for c in a["courses"]} & {c["id"] for c in b["courses"]})
    assert "credentials" not in a["account"]
    assert client.get("/api/accounts/missing/courses").status_code == 404


def test_accounts_run_asynchronously_and_task_filters_combine(client, monkeypatch):
    monkeypatch.setenv("DEMO_TICK_SECONDS", "0.15")
    first, course_a = demo(client)
    second = client.post("/api/demo?new_account=true").json()["account_id"]
    course_b = next(c for c in client.get(f"/api/accounts/{second}/courses").json()["courses"] if c["done"] < c["total"])
    ids = client.post("/api/tasks", json={"course_ids": [course_a["id"], course_b["id"]]}).json()["task_ids"]
    for task_id in ids:
        wait_for(client, task_id, ["running"])
    assert server.manager.processes[ids[0]].pid != server.manager.processes[ids[1]].pid
    # Loading B's courses must leave A's process and task untouched.
    assert client.get(f"/api/accounts/{second}/courses").status_code == 200
    assert task(client, ids[0])["status"] == "running"
    all_running = client.get("/api/tasks").json()["tasks"]
    assert {t["account_id"] for t in all_running} == {first, second}
    filtered = client.get("/api/tasks", params={"account_id": second, "course_id": course_b["id"]}).json()["tasks"]
    assert [t["id"] for t in filtered] == [ids[1]]
    assert not client.get("/api/tasks", params={"account_id": first, "course_id": course_b["id"]}).json()["tasks"]
    assert client.get("/api/tasks?status=unknown").status_code == 400
    assert client.post(f"/api/tasks/{ids[0]}/control", json={"action": "cancel"}).status_code == 200
    wait_for(client, ids[0], ["cancelled"])
    assert task(client, ids[1])["status"] == "running"
    assert client.post(f"/api/tasks/{ids[1]}/control", json={"action": "cancel"}).status_code == 200


def test_old_history_cannot_hide_running_tasks(client):
    server.manager.stop.set()
    server.manager.thread.join(timeout=3)
    _, course = demo(client)
    task_id = client.post("/api/tasks", json={"course_ids": [course["id"]]}).json()["task_ids"][0]
    with store.db() as conn:
        source = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        for index in range(305):
            conn.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?)", (f"history-{index}", source["account_id"], source["course_id"], source["title"], "server", "completed", 100,
                         '{"status":"completed"}', "2099-01-01", "2099-01-01"))
    assert task_id in {t["id"] for t in client.get("/api/dashboard").json()["tasks"]}
    assert [t["id"] for t in client.get("/api/tasks").json()["tasks"]] == [task_id]


def test_platform_course_order_is_preserved_and_updated(client, monkeypatch):
    account_id, _ = demo(client)
    response = client.get(f"/api/accounts/{account_id}/courses").json()
    assert [c["title"] for c in response["courses"]] == ["大学英语 · 读写进阶", "人工智能与未来生活", "中国传统文化", "大学生心理健康"]
    import json
    raw = [json.loads(row["raw"]) for row in store.rows("SELECT raw FROM courses WHERE account_id=? ORDER BY source_position DESC", (account_id,))]
    fake = Mock(returncode=0, stdout=json.dumps({"courses": raw}))
    monkeypatch.setattr(server.subprocess, "run", lambda *_a, **_k: fake)
    assert client.post(f"/api/accounts/{account_id}/sync").status_code == 200
    titles = [c["title"] for c in client.get(f"/api/accounts/{account_id}/courses").json()["courses"]]
    assert titles == [c["title"] for c in raw]


def test_progress_refresh_runs_without_starting_a_study_task(client):
    _, course = demo(client)
    assert client.post(f"/api/courses/{course['id']}/progress").status_code == 202
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        snapshot = client.get(f"/api/courses/{course['id']}").json()
        if snapshot["course"]["progress_updated_at"]:
            break
        time.sleep(0.05)
    assert snapshot["course"]["task_total"] == course["total"]
    assert snapshot["course"]["task_done"] == course["done"]
    assert len(snapshot["chapters"]) == course["total"]
    assert client.get("/api/tasks?status=all").json()["tasks"] == []


def test_quiz_config_secrets_are_encrypted_and_never_returned(client):
    config = {"provider": "AI", "endpoint": "https://example.invalid/v1", "model": "test-model", "key": "private-ai-key"}
    assert client.put("/api/quiz", json=config).status_code == 200
    assert client.get('/api/dashboard').json()['quiz']['attempts'] == 1
    assert "private-ai-key" not in client.get("/api/dashboard").text
    assert "private-ai-key" not in store.one("SELECT value FROM settings WHERE key='quiz'")["value"]
    config["key"] = ""
    assert client.put("/api/quiz", json=config).status_code == 200
    assert store.setting("quiz")["key"] == "private-ai-key"


def test_demo_quiz_waits_for_valid_answers_and_continues(client, monkeypatch):
    monkeypatch.setenv("STUDY_DEMO_QUIZ", "1")
    _, course = demo(client)
    task_id = client.post("/api/tasks", json={"course_ids": [course["id"]]}).json()["task_ids"][0]
    waiting = wait_for(client, task_id, ["waiting_answer"], timeout=15)
    pending = waiting["state"]["pending_quiz"]
    assert task_id in {t["id"] for t in client.get("/api/tasks").json()["tasks"]}
    invalid = {"quiz_id": pending["id"], "answers": {"demo-question": "Z"}}
    assert client.post(f"/api/tasks/{task_id}/answers", json=invalid).status_code == 400
    assert task(client, task_id)["status"] == "waiting_answer"
    valid = {"quiz_id": pending["id"], "answers": {"demo-question": "B"}}
    assert client.post(f"/api/tasks/{task_id}/answers", json=valid).status_code == 202
    assert wait_for(client, task_id, ["completed"], timeout=15)["progress"] == 100
    assert client.post(f"/api/tasks/{task_id}/answers", json=valid).status_code == 409


def test_failed_progress_refresh_preserves_existing_snapshot(client):
    server.manager.stop.set()
    server.manager.thread.join(timeout=3)
    _, course = demo(client)
    store.execute("UPDATE courses SET progress_status='running',progress_updated_at='2026-11-01',progress_json=? WHERE id=?",
                  ('{"task_total":12,"task_done":3}', course["id"]))
    path = store.DATA / "progress" / "test-refresh"
    path.mkdir(parents=True)
    store.write_json(path / "state.json", {"error": "登录失效"})
    process = Mock(returncode=2)
    process.poll.return_value = 2
    server.manager.progress_processes[course["id"]] = (process, path, time.monotonic())
    server.manager.tick_progress()
    record = client.get(f"/api/courses/{course['id']}").json()["course"]
    assert record["progress_status"] == "error"
    assert record["progress_updated_at"] == "2026-11-01"
    assert record["task_done"] == 3
    assert record["done"] == course["done"]
    assert record["progress_error"] == "登录失效"


@pytest.mark.parametrize('heartbeat_newer', [False, True])
def test_stale_worker_report_cannot_overwrite_a_newer_manual_refresh(client, heartbeat_newer):
    server.manager.stop.set()
    server.manager.thread.join(timeout=3)
    _, course = demo(client)
    task_id = client.post("/api/tasks", json={"course_ids": [course["id"]]}).json()["task_ids"][0]
    store.execute("UPDATE courses SET progress_updated_at='2026-11-14T00:00:00+00:00' WHERE id=?", (course["id"],))
    report = {"status": "running", "total": course["total"], "done": 0, "updated_at": "2026-11-13T00:00:00+00:00"}
    if heartbeat_newer:
        report.update(updated_at="2026-11-15T00:00:00+00:00", platform_progress_at="2026-11-13T00:00:00+00:00")
    update_task(task_id, report)
    assert store.one("SELECT done FROM courses WHERE id=?", (course["id"],))["done"] == course["done"]
    report.update(done=course['total'] - 1, platform_progress_at="2026-11-15T00:00:00+00:00")
    update_task(task_id, report)
    assert store.one("SELECT done FROM courses WHERE id=?", (course["id"],))["done"] == course['total'] - 1


def test_demo_transient_failure_automatically_recovers_in_real_worker(client, monkeypatch):
    monkeypatch.setenv("STUDY_DEMO_RETRY_ONCE", "1")
    assert client.put("/api/quiz", json={"provider": "TikuGo", "retry_delay": 2}).status_code == 200
    _, course = demo(client)
    task_id = client.post("/api/tasks", json={"course_ids": [course["id"]]}).json()["task_ids"][0]
    waiting = wait_for(client, task_id, ["retry_wait"])
    assert waiting["state"]["retry_count"] == 1
    assert "pending_quiz" not in waiting["state"] or waiting["state"]["pending_quiz"] is None
    assert client.post("/api/tasks", json={"course_ids": [course["id"]]}).status_code == 409
    completed = wait_for(client, task_id, ["completed"], timeout=15)
    assert completed["state"]["retry_count"] == 1


def test_deferred_worker_frees_slot_and_can_be_cancelled(client):
    server.manager.stop.set()
    server.manager.thread.join(timeout=3)
    _, course = demo(client)
    other = next(c for c in client.get("/api/dashboard").json()["courses"] if c["id"] != course["id"])
    client.put("/api/settings", json={"concurrency": 1})
    first = client.post("/api/tasks", json={"course_ids": [course["id"]]}).json()["task_ids"][0]
    second = client.post("/api/tasks", json={"course_ids": [other["id"]]}).json()["task_ids"][0]
    path = store.DATA / "jobs" / first
    path.mkdir()
    store.write_json(path / "state.json", {"status": "retry_wait", "next_retry_at": "2099-01-01", "retry_count": 1})
    process = Mock()
    process.poll.return_value = 3
    server.manager.processes[first] = process
    server.manager.tick()
    assert first not in server.manager.processes
    assert second in server.manager.processes
    assert task(client, first)["status"] == "retry_wait"
    assert client.post(f"/api/tasks/{first}/control", json={"action": "cancel"}).status_code == 200
    assert task(client, first)["status"] == "cancelled"
    assert second in server.manager.processes


def test_retry_configuration_validation_and_all_secrets_remain_private(client):
    config = {"provider": "TikuLike", "like_tokens": "private-like-token", "siliconflow_key": "private-silicon-key", "fallbacks": ["SiliconFlow"]}
    assert client.put("/api/quiz", json=config).status_code == 200
    text = client.get("/api/dashboard").text
    assert "private-like-token" not in text and "private-silicon-key" not in text
    assert client.put("/api/quiz", json={"provider": "TikuGo", "fallbacks": ["invalid"]}).status_code == 400
    assert client.put("/api/quiz", json={"provider": "manual", "fallbacks": []}).status_code == 400
    assert client.put("/api/quiz", json={"provider": "TikuGo", "attempts": 0}).status_code == 422
    assert client.put("/api/quiz", json={"provider": "TikuGo", "attempts": 4}).status_code == 422
    for count in (3, 2, 1):
        assert client.put('/api/quiz', json={'provider': 'TikuGo', 'attempts': count}).status_code == 200
        assert client.get('/api/dashboard').json()['quiz']['attempts'] == count


def test_cancel_racing_with_retry_exit_cannot_reschedule(client):
    server.manager.stop.set()
    server.manager.thread.join(timeout=3)
    _, course = demo(client)
    task_id = client.post("/api/tasks", json={"course_ids": [course["id"]]}).json()["task_ids"][0]
    path = store.DATA / "jobs" / task_id
    path.mkdir()
    store.write_json(path / "state.json", {"status": "retry_wait", "next_retry_at": "2099-01-01"})
    process = Mock()
    process.poll.return_value = None
    server.manager.processes[task_id] = process
    update_task(task_id, {"status": "cancelling"})
    server.manager.tick()
    assert task(client, task_id)["status"] == "cancelling"
    process.poll.return_value = 3
    server.manager.tick()
    assert task(client, task_id)["status"] == "cancelled"
    assert task_id not in server.manager.processes


def test_ai_connection_check_does_not_save_or_leak_key(client, monkeypatch):
    from app import quiz
    assert client.post('/api/quiz/test', json={'provider':'TikuGo'}).status_code == 400
    seen = []
    def model(name, question, config):
        seen.append(question)
        return {'Answer':['B']}
    monkeypatch.setattr(quiz,'query_ai',model)
    body = {'provider':'TikuGo','endpoint':'https://example.invalid/v1','model':'test','key':'private-connection-test-key'}
    result = client.post('/api/quiz/test',json=body)
    assert result.status_code == 200 and result.json()['ok']
    assert 'private-connection-test-key' not in result.text
    assert store.setting('quiz',{}) == {}
    assert seen[0]['title'].startswith('连接测试')
    monkeypatch.setattr(quiz,'query_ai',lambda *_: {'Answer':[]})
    assert client.post('/api/quiz/test',json=body).status_code == 502
    assert client.put('/api/quiz',json={'provider':'AI','endpoint':'http://127.0.0.1:11434/v1','model':'local'}).status_code == 200


def test_saving_ai_setup_wakes_deferred_tasks(client):
    server.manager.stop.set();server.manager.thread.join(timeout=3)
    _, course = demo(client)
    task_id=client.post('/api/tasks',json={'course_ids':[course['id']]}).json()['task_ids'][0]
    update_task(task_id,{'status':'retry_wait','retry_count':1,'next_retry_at':'2099-01-01'})
    body={'provider':'TikuGo','endpoint':'https://example.invalid/v1','model':'test','key':'private-key'}
    assert client.put('/api/quiz',json=body).status_code == 200
    assert task(client,task_id)['state']['next_retry_at'] <= store.now()
    sources=client.get('/api/dashboard').json()['answer_sources']
    assert sources['ai_ready'] and sources['chain'] == ['TikuGo','AI']


def test_point_concurrency_routes_preserve_live_target_and_validate_bounds(client):
    from app.manager import payload_for
    server.manager.stop.set();server.manager.thread.join(timeout=3)
    _, course=demo(client)
    assert client.get('/api/dashboard').json()['point_concurrency']==1
    response=client.post('/api/tasks',json={'course_ids':[course['id']],'point_concurrency':3,'task_point_limit':3})
    task_id=response.json()['task_ids'][0]
    for count in (2,1,3):
        assert client.put(f'/api/tasks/{task_id}/point-concurrency',json={'concurrency':count}).status_code==200
        assert payload_for(store.one('SELECT * FROM tasks WHERE id=?',(task_id,)))['point_concurrency']==count
    for count in (0,4):
        assert client.put(f'/api/tasks/{task_id}/point-concurrency',json={'concurrency':count}).status_code==422
    # An old worker state file cannot erase the latest runtime target on retry.
    value=task(client,task_id)['state'];value['point_concurrency']=1;update_task(task_id,value)
    assert payload_for(store.one('SELECT * FROM tasks WHERE id=?',(task_id,)))['point_concurrency']==3
    assert client.put('/api/settings',json={'concurrency':2,'point_concurrency':2}).status_code==200
    assert payload_for(store.one('SELECT * FROM tasks WHERE id=?',(task_id,)))['point_concurrency']==2
    update_task(task_id,{'status':'completed','point_concurrency':2})
    assert client.put(f'/api/tasks/{task_id}/point-concurrency',json={'concurrency':1}).status_code==409


def test_manual_answers_are_routed_to_the_correct_point_process(client):
    server.manager.stop.set();server.manager.thread.join(timeout=3)
    _, course=demo(client)
    task_id=client.post('/api/tasks',json={'course_ids':[course['id']]}).json()['task_ids'][0]
    unit_id='a'*24;quiz_id='b'*32
    pending={'id':quiz_id,'unit_id':unit_id,'questions':[{'id':'q','type':'single','options':'A. one\nB. two'}]}
    value={'status':'waiting_answer','pending_quiz':pending}
    update_task(task_id,value)
    path=store.DATA/'jobs'/task_id/'units'/unit_id;path.mkdir(parents=True)
    store.write_json(path/'state.json',value)
    body={'quiz_id':quiz_id,'answers':{'q':'B'}}
    assert client.post(f'/api/tasks/{task_id}/answers',json=body).status_code==202
    assert (path/f'answers-{quiz_id}.json').exists()
    assert not (path.parent.parent/f'answers-{quiz_id}.json').exists()
    store.write_json(path/'state.json',{'status':'running'})
    assert client.post(f'/api/tasks/{task_id}/answers',json=body).status_code==409


def test_crashed_course_process_cannot_leave_children_holding_claims(client,monkeypatch):
    import fcntl,os
    monkeypatch.setenv('DEMO_TICK_SECONDS','1')
    _, course=demo(client)
    task_id=client.post('/api/tasks',json={'course_ids':[course['id']],'point_concurrency':3}).json()['task_ids'][0]
    deadline=time.monotonic()+12;value={}
    while time.monotonic()<deadline:
        value=task(client,task_id)
        if value['state'].get('active_point_count')==3:break
        time.sleep(.05)
    assert value['state'].get('active_point_count')==3
    keys=[p['id'] for p in value['state']['active_points']]
    parent=server.manager.processes[task_id];parent.kill();parent.wait(timeout=3)
    with server.manager.lock:server.manager.tick()
    assert task(client,task_id)['status']=='interrupted'
    folder=(store.DATA/'point-state'/(course['id']+'.sqlite')).with_suffix('.locks')
    for key in keys:
        fd=os.open(folder/(key+'.lock'),os.O_RDWR)
        deadline=time.monotonic()+3
        try:
            while True:
                try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB);break
                except BlockingIOError:
                    assert time.monotonic()<deadline,'Orphan process retained point claim'
                    time.sleep(.05)
        finally:os.close(fd)


def test_retry_now_requeues_same_task_and_preserves_recovery_data(client):
    server.manager.stop.set()
    server.manager.thread.join(timeout=3)
    _, course = demo(client)
    tid = client.post('/api/tasks', json={'course_ids': [course['id']]}).json()['task_ids'][0]
    state = {'status': 'retry_wait', 'retry_count': 2, 'next_retry_at': '2099-01-01', 'progress': 25,
             'error': 'temporary failure', 'point_history': [{'id': 'verified', 'status': 'completed'}], 'logs': []}
    update_task(tid, state)
    journal = store.DATA / 'quiz-state' / (course['id'] + '.json')
    journal.parent.mkdir(exist_ok=True)
    journal.write_text('{"work":{"phase":"intent"}}')
    before = store.one('SELECT * FROM tasks WHERE id=?', (tid,))
    assert client.get('/api/dashboard').json()['capabilities']['retry_now'] is True
    result = client.post(f'/api/tasks/{tid}/retry-now')
    assert result.status_code == 202 and result.json()['task_id'] == tid
    after = store.one('SELECT * FROM tasks WHERE id=?', (tid,))
    value = json.loads(after['state'])
    assert after['status'] == 'queued' and value['next_retry_at'] is None
    assert value['retry_count'] == 2 and value['point_history'] == state['point_history']
    assert after['created_at'] == before['created_at'] and after['progress'] == 25
    assert journal.read_text() == '{"work":{"phase":"intent"}}'
    assert len(store.rows('SELECT id FROM tasks')) == 1
    assert client.post(f'/api/tasks/{tid}/retry-now').status_code == 409
    # The previous process must have exited before its old state can be replaced.
    update_task(tid, state)
    server.manager.processes[tid] = Mock()
    assert client.post(f'/api/tasks/{tid}/retry-now').status_code == 409
    server.manager.processes.clear()
    for status in ('running', 'paused', 'waiting_answer', 'completed', 'cancelled', 'attention'):
        update_task(tid, {**state, 'status': status})
        assert client.post(f'/api/tasks/{tid}/retry-now').status_code == 409
