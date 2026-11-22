import json
import subprocess
import sys
import threading
import time
import uuid
import shutil
import os
import signal
from datetime import datetime, timezone, timedelta

from app import store
from app.runtime import worker_command, worker_popen_options

ACTIVE = ("queued", "running", "paused", "cancelling", "waiting_answer", "retry_wait")
TERMINAL = ("completed", "cancelled", "attention", "failed", "interrupted")


def public_task(task):
    state = json.loads(task["state"])
    return {**task, "state": state}


def update_task(task_id, state):
    store.execute("UPDATE tasks SET status=?,progress=?,state=?,updated_at=? WHERE id=?",
        (state["status"], state.get("progress", 0), json.dumps(state, ensure_ascii=False), store.now(), task_id))
    task = store.one("SELECT * FROM tasks WHERE id=?", (task_id,))
    if "total" in state and state["total"]:
        store.execute("UPDATE courses SET total=?,done=? WHERE id=? AND (progress_updated_at IS NULL OR progress_updated_at<=?)",
                      (state["total"], state.get("done", 0), task["course_id"],
                       state.get("platform_progress_at", state.get("updated_at", ""))))


def payload_for(task):
    account = store.one("SELECT * FROM accounts WHERE id=?", (task["account_id"],))
    course = store.one("SELECT * FROM courses WHERE id=?", (task["course_id"],))
    raw = json.loads(course["raw"])
    if account["mode"] == "demo":
        raw.update(total=course["total"], done=course["done"])
    state = json.loads(task['state'])
    runtime_path = store.DATA / 'jobs' / task['id'] / 'runtime.json'
    runtime = store.read_json(runtime_path, {})
    return {"action": "learn", "task_id": task["id"], "mode": account["mode"],
            "credentials": store.unseal(account["credentials"]), "course": raw,
            "quiz_config": store.setting("quiz", {}), "previous_state": state,
            "point_concurrency": runtime.get('point_concurrency', state.get('point_concurrency', store.setting('point_concurrency', 1))),
            "task_point_limit": state.get('task_point_limit'), 'runtime_path': str(runtime_path),
            "point_store": str(store.DATA / 'point-state' / (task['course_id'] + '.sqlite')),
            "quiz_journal": str(store.DATA / "quiz-state" / (task["course_id"] + ".json"))}


def cleanup_group(process):
    if getattr(process, '_study_group', False) is True:
        if os.name == 'nt':
            if process.poll() is None:
                subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
            return
        if hasattr(process, 'safe_group') and not process.safe_group():
            return
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


class Manager:
    def __init__(self):
        self.processes = {}
        self.stop = threading.Event()
        self.lock = threading.RLock()
        self.progress_processes = {}
        self.updating = False

    def start(self):
        store.execute("UPDATE courses SET progress_status='error',progress_error='上次刷新被应用退出中断，可重新刷新' WHERE progress_status IN ('queued','running')")
        for task in store.rows("SELECT * FROM tasks WHERE engine='server' AND status IN ('running','paused','cancelling','waiting_answer')"):
            state = json.loads(task["state"])
            from app.process_recovery import AttachedWorker, InspectionUnavailable
            path = store.DATA / 'jobs' / task['id']
            latest = store.read_json(path / 'state.json', {})
            try:
                process = AttachedWorker.attach(latest.get('pid'), path)
            except InspectionUnavailable:
                state.update(status='attention', error='无法核验原工作进程，暂不重复启动；请确认原进程状态后重试', next_retry_at=None)
                update_task(task['id'], state)
                continue
            if process is not None:
                self.processes[task['id']] = process
                latest = {**state, **latest}
                if task['status'] == 'paused' and latest['status'] in ('running', 'waiting_answer'):
                    latest['status'] = 'paused'
                elif task['status'] == 'cancelling':
                    latest['status'] = 'cancelling'
                    store.write_json(path / 'control.json', {'action': 'cancel'})
                    process.terminate()
                latest.setdefault('logs', []).append({'time': store.now(), 'level': 'info',
                    'message': '控制台已重新接管原工作进程，保留任务点进程和执行进度'})
                latest['logs'] = latest['logs'][-200:]
                update_task(task['id'], latest)
                continue
            from app.quiz import DEFAULT_CONFIG
            config = {**DEFAULT_CONFIG, **store.setting("quiz", {})}
            if task["status"] == "cancelling":
                state.update(status="cancelled", chapter="任务已停止")
            elif task["status"] in ("running", "waiting_answer") and config["mode"] == "unattended":
                count = state.get("retry_count", 0) + 1
                if count > config["max_retries"]:
                    state.update(status="attention", error="应用退出后的自动恢复次数已用完", next_retry_at=None)
                else:
                    state.update(status="retry_wait", retry_count=count, error="应用曾退出，稍后自动恢复并核验平台进度",
                        next_retry_at=(datetime.now(timezone.utc) + timedelta(seconds=config["retry_delay"])).isoformat(),
                        chapter="等待自动恢复")
            else:
                state.update(status="interrupted", error="本地应用曾退出；点击重试将从平台进度继续")
            update_task(task["id"], state)
        self.thread = threading.Thread(target=self.loop, daemon=True)
        self.thread.start()

    def shutdown(self):
        self.stop.set()
        self.thread.join(timeout=45)
        with self.lock:
            processes = list(self.processes.values()) + [item[0] for item in self.progress_processes.values()]
            for process in processes:
                if process.poll() is None:
                    process.terminate()
            for process in processes:
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
                cleanup_group(process)

    def loop(self):
        while not self.stop.wait(1):
            try:
                with self.lock:
                    self.tick()
                    self.tick_progress()
            except Exception:
                # A single job/network problem must not stop the scheduler.
                pass

    def tick(self):
        for task_id, process in list(self.processes.items()):
            task = store.one("SELECT * FROM tasks WHERE id=?", (task_id,))
            state = store.read_json(store.DATA / "jobs" / task_id / "state.json")
            exit_code = process.poll()
            if state:
                if exit_code is not None and task["status"] == "cancelling":
                    state.update(status="cancelled", chapter="任务已停止", next_retry_at=None)
                elif exit_code is not None and state["status"] not in (*TERMINAL, "retry_wait"):
                    if task["status"] == "cancelling":
                        state.update(status="cancelled", chapter="任务已停止")
                    else:
                        state.update(status="interrupted", error="工作进程意外退出，可点击重试")
                elif task["status"] == "paused" and state["status"] in ("running", "waiting_answer"):
                    state["status"] = "paused"
                elif task["status"] == "cancelling" and state["status"] not in TERMINAL:
                    state["status"] = "cancelling"
                update_task(task_id, state)
            elif exit_code is not None:
                if task["status"] == "cancelling":
                    # Cancellation can beat worker initialization and its first state file.
                    state = json.loads(task["state"])
                    state.update(status="cancelled", chapter="任务已停止")
                    update_task(task_id, state)
                else:
                    update_task(task_id, {"status": "failed", "error": "工作进程启动失败，请检查依赖", "logs": []})
            if exit_code is not None:
                cleanup_group(process)
                (store.DATA / "jobs" / task_id / "cookies.txt").unlink(missing_ok=True)
                del self.processes[task_id]
        for task in store.rows("SELECT * FROM tasks WHERE engine='server' AND status='retry_wait'"):
            state = json.loads(task["state"])
            if state.get("next_retry_at", "9999") <= store.now():
                state.update(status="queued", chapter="自动重试已到期，等待运行", error=None)
                update_task(task["id"], state)
        if self.updating:
            return
        limit = store.setting("concurrency", 3)
        free = max(0, limit - len(self.processes))
        for task in store.rows("SELECT * FROM tasks WHERE engine='server' AND status='queued' ORDER BY created_at LIMIT ?", (free,)):
            path = store.DATA / "jobs" / task["id"]
            path.mkdir(exist_ok=True)
            (path / "state.json").unlink(missing_ok=True)
            store.write_json(path / "control.json", {"action": "run"})
            payload = payload_for(task)
            store.write_json(path / 'runtime.json', {'point_concurrency': payload['point_concurrency']})
            process = subprocess.Popen(worker_command('--study-task-id=' + task['id']),
                cwd=path, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                **worker_popen_options())
            process._study_group = True
            process.stdin.write(json.dumps(payload, ensure_ascii=False).encode())
            process.stdin.close()
            self.processes[task["id"]] = process
            state = json.loads(task["state"])
            state.update(status="running", chapter="正在启动独立工作进程")
            update_task(task["id"], state)

    def control(self, task, action):
        if task["status"] not in ACTIVE:
            raise ValueError("此任务已经结束")
        if action == "pause" and task["status"] not in ("running", "waiting_answer"):
            raise ValueError("只有运行中的任务可以暂停")
        if action == "run" and task["status"] != "paused":
            raise ValueError("只有暂停的任务可以继续")
        state = json.loads(task["state"])
        if task["engine"] != "server":
            raise ValueError("这是旧版云端任务，请在原平台管理；本地版只控制本机任务")
        path = store.DATA / "jobs" / task["id"]
        path.mkdir(exist_ok=True)
        store.write_json(path / "control.json", {"action": action})
        if action == "cancel":
            process = self.processes.get(task["id"])
            if process and process.poll() is None:
                process.terminate()
        status = {"run": "running", "pause": "paused", "cancel": "cancelling"}[action]
        if action == "cancel" and task["engine"] == "server" and task["id"] not in self.processes:
            status = "cancelled"
        state["status"] = status
        update_task(task["id"], state)

    def request_progress(self, course_id):
        with self.lock:
            course = store.one("SELECT * FROM courses WHERE id=?", (course_id,))
            if course["progress_status"] in ("queued", "running"):
                return course["progress_status"]
            store.execute("UPDATE courses SET progress_status='queued',progress_error='' WHERE id=?", (course_id,))
            return "queued"

    def cancel_account_progress(self, account_id):
        for course in store.rows("SELECT id FROM courses WHERE account_id=?", (account_id,)):
            item = self.progress_processes.pop(course["id"], None)
            if item:
                process, path, _ = item
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=5)
                shutil.rmtree(path, ignore_errors=True)

    def tick_progress(self):
        for course_id, (process, path, started) in list(self.progress_processes.items()):
            timed_out = time.monotonic() - started > 600
            if timed_out and process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            if process.poll() is None:
                continue
            snapshot = store.read_json(path / "snapshot.json")
            state = store.read_json(path / "state.json", {})
            course = store.one("SELECT * FROM courses WHERE id=?", (course_id,))
            if course:
                if process.returncode == 0 and snapshot:
                    store.execute("UPDATE courses SET total=?,done=?,progress_json=?,progress_updated_at=?,progress_status='idle',progress_error='' WHERE id=?",
                        (snapshot["total"], snapshot["done"], json.dumps(snapshot, ensure_ascii=False), store.now(), course_id))
                else:
                    error = "刷新超时，原有进度已保留" if timed_out else state.get("error", "任务点读取失败，原有进度已保留")
                    store.execute("UPDATE courses SET progress_status='error',progress_error=? WHERE id=?", (error, course_id))
            shutil.rmtree(path, ignore_errors=True)
            del self.progress_processes[course_id]
        free = max(0, 2 - len(self.progress_processes))
        for course in store.rows("SELECT * FROM courses WHERE progress_status='queued' ORDER BY rowid LIMIT ?", (free,)):
            account = store.one("SELECT * FROM accounts WHERE id=?", (course["account_id"],))
            path = store.DATA / "progress" / uuid.uuid4().hex
            path.mkdir(parents=True)
            raw = json.loads(course["raw"])
            if account["mode"] == "demo":
                raw.update(total=course["total"], done=course["done"])
            payload = {"action": "inspect", "mode": account["mode"], "course": raw,
                       "credentials": store.unseal(account["credentials"]),
                       'point_store': str(store.DATA / 'point-state' / (course['id'] + '.sqlite'))}
            process = subprocess.Popen(worker_command('--study-progress-id=' + course['id']),
                cwd=path, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                **worker_popen_options())
            process.stdin.write(json.dumps(payload, ensure_ascii=False).encode())
            process.stdin.close()
            self.progress_processes[course["id"]] = (process, path, time.monotonic())
            store.execute("UPDATE courses SET progress_status='running' WHERE id=?", (course["id"],))


manager = Manager()
