import hashlib
import hmac
import json
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import asynccontextmanager
from urllib.parse import urlparse

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app import store, updater
from app.manager import ACTIVE, manager, public_task, update_task
from app.runtime import MAX_POINT_CONCURRENCY, worker_command

SERVER_VERSION = updater.build_info()["version"]


@asynccontextmanager
async def lifespan(app):
    store.initialize()
    with store.runtime_lock():
        manager.start()
        try:
            yield
        finally:
            manager.shutdown()


app = FastAPI(title="课栈 · 学习控制台", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=store.ROOT / "static"), name="static")
attempts = {}
mutations = 0


@app.middleware("http")
async def protect(request: Request, call_next):
    path = request.url.path
    if path.startswith("/api/") and path not in ("/api/auth", "/api/health"):
        if not hmac.compare_digest(request.cookies.get("study_session", "").encode(), store.session_token().encode()):
            return JSONResponse({"detail": "请先输入控制台访问口令"}, status_code=401)
    if path.startswith("/api/") and request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin")
        if origin and urlparse(origin).netloc != request.headers.get("host"):
            return JSONResponse({"detail": "请求来源不匹配"}, status_code=403)
    global mutations
    changes = path.startswith("/api/") and request.method not in ("GET", "HEAD", "OPTIONS") and path != "/api/updates/install"
    if changes and manager.updating:
        return JSONResponse({"detail": "正在切换版本，请稍后重试"}, status_code=409)
    if changes:
        mutations += 1
    try:
        response = await call_next(request)
    finally:
        if changes:
            mutations -= 1
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'"
    if path.startswith("/api/") or path == "/" or path in ("/static/app.js", "/static/style.css"):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.exception_handler(ValueError)
async def invalid(request, error):
    return JSONResponse({"detail": str(error)}, status_code=400)


@app.exception_handler(RuntimeError)
async def unavailable(request, error):
    return JSONResponse({"detail": str(error)}, status_code=502)


class Auth(BaseModel):
    token: str = Field(min_length=1, max_length=200)


@app.post("/api/auth")
def login(body: Auth, request: Request, response: Response):
    ip = request.client.host
    count, start = attempts.get(ip, (0, time.monotonic()))
    if time.monotonic() - start > 60:
        count, start = 0, time.monotonic()
    if count >= 10:
        raise HTTPException(429, "尝试过多，请一分钟后重试")
    if not hmac.compare_digest(body.token.encode(), store.token().encode()):
        attempts[ip] = (count + 1, start)
        raise HTTPException(401, "访问口令不正确")
    attempts.pop(ip, None)
    secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    response.set_cookie("study_session", store.session_token(), httponly=True, samesite="strict", secure=secure, max_age=604800)
    return {"ok": True}


@app.post("/api/logout")
def logout(response: Response):
    response.delete_cookie("study_session")
    return {"ok": True}


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/")
def index():
    return FileResponse(store.ROOT / "static" / "index.html")


def get_account(account_id):
    account = store.one("SELECT * FROM accounts WHERE id=?", (account_id,))
    if not account:
        raise HTTPException(404, "账号不存在")
    return account


def account_public(account):
    return {key: account[key] for key in ("id", "name", "hint", "mode", "status", "created_at")}


@app.get("/api/accounts/{account_id}/courses")
def account_courses(account_id: str):
    account = get_account(account_id)
    courses = store.rows("SELECT * FROM courses WHERE account_id=? ORDER BY source_position,rowid", (account_id,))
    return {"account": account_public(account), "courses": [store.public_course(c) for c in courses]}


def get_course(course_id):
    course = store.one("SELECT * FROM courses WHERE id=?", (course_id,))
    if not course:
        raise HTTPException(404, "课程不存在")
    return course


@app.get("/api/courses/{course_id}")
def course_detail(course_id: str):
    course = get_course(course_id)
    return {"course": store.public_course(course), "chapters": json.loads(course["progress_json"]).get("chapters", [])}


@app.post("/api/courses/{course_id}/progress", status_code=202)
def refresh_course_progress(course_id: str):
    get_course(course_id)
    return {"status": manager.request_progress(course_id)}


@app.post("/api/accounts/{account_id}/progress", status_code=202)
def refresh_account_progress(account_id: str):
    get_account(account_id)
    courses = store.rows("SELECT id FROM courses WHERE account_id=? ORDER BY source_position,rowid", (account_id,))
    for course in courses:
        manager.request_progress(course["id"])
    return {"count": len(courses)}


@app.get("/api/tasks")
def list_tasks(account_id: str | None = None, course_id: str | None = None, status: str = "active"):
    conditions, values = [], []
    if account_id:
        get_account(account_id)
        conditions.append("account_id=?")
        values.append(account_id)
    if course_id:
        conditions.append("course_id=?")
        values.append(course_id)
    if status == "active":
        conditions.append("status IN ('queued','running','paused','cancelling','waiting_answer','retry_wait')")
    elif status == "attention":
        conditions.append("status IN ('attention','failed','interrupted')")
    elif status != "all":
        if status not in ("completed", "cancelled", *ACTIVE):
            raise HTTPException(400, "未知的任务状态")
        conditions.append("status=?")
        values.append(status)
    where = " WHERE " + " AND ".join(conditions) if conditions else ""
    return {"tasks": [public_task(task) for task in store.rows("SELECT * FROM tasks" + where + " ORDER BY created_at DESC", values)]}


def get_task(task_id):
    task = store.one("SELECT * FROM tasks WHERE id=?", (task_id,))
    if not task:
        raise HTTPException(404, "任务不存在")
    return task


@app.get("/api/dashboard")
def dashboard():
    accounts = store.rows("SELECT * FROM accounts ORDER BY created_at")
    courses = store.rows("SELECT * FROM courses ORDER BY account_id,source_position,rowid")
    tasks = store.rows("SELECT * FROM tasks WHERE status IN ('queued','running','paused','cancelling','waiting_answer','retry_wait') OR id IN (SELECT id FROM tasks ORDER BY created_at DESC LIMIT 300) ORDER BY created_at DESC")
    from app.quiz import DEFAULT_CONFIG, PUBLIC_KEYS, SECRET_KEYS, source_order, source_available
    quiz = {**DEFAULT_CONFIG, **store.setting("quiz", {})}
    return {"accounts": [account_public(a) for a in accounts], "courses": [store.public_course(c) for c in courses],
            "tasks": [public_task(t) for t in tasks], "concurrency": store.setting("concurrency", 3),
            "point_concurrency": store.setting('point_concurrency', 1),
            "capabilities": {"retry_now": True, "portable_updates": updater.portable(),
                             "max_point_concurrency": MAX_POINT_CONCURRENCY}, "version": SERVER_VERSION,
            "execution_mode": "local", "quiz": {key: quiz[key] for key in PUBLIC_KEYS},
            "quiz_secrets": {key: bool(quiz[key]) for key in SECRET_KEYS},
            "answer_sources": {"chain": [name for name in source_order(quiz) if source_available(name, quiz)],
                               "ai_ready": source_available("AI", quiz) or source_available("SiliconFlow", quiz)}}


class AccountBody(BaseModel):
    name: str = Field(min_length=1, max_length=50)
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=200)


@app.post("/api/accounts", status_code=201)
def add_account(body: AccountBody):
    username = body.username.strip()
    if not username or not body.name.strip():
        raise HTTPException(400, "账号与备注不能只包含空格")
    account_id = uuid.uuid4().hex
    hint = username[:3] + "****" + username[-4:] if len(username) >= 8 else username[:1] + "***"
    try:
        store.execute("INSERT INTO accounts VALUES (?,?,?,?,?,?,?,?)", (
            account_id, body.name.strip(), hint, hashlib.sha256(username.encode()).hexdigest(),
            store.seal({"username": username, "password": body.password}), "real", "saved", store.now()))
    except sqlite3.IntegrityError:
        raise HTTPException(409, "该账号已经保存，请选择已有账号")
    return account_public(get_account(account_id))


class PasswordBody(BaseModel):
    password: str = Field(min_length=1, max_length=200)


@app.patch("/api/accounts/{account_id}")
def change_password(account_id: str, body: PasswordBody):
    account = get_account(account_id)
    credentials = store.unseal(account["credentials"])
    credentials["password"] = body.password
    store.execute("UPDATE accounts SET credentials=?,status='saved' WHERE id=?", (store.seal(credentials), account_id))
    return {"ok": True}


@app.delete("/api/accounts/{account_id}")
def delete_account(account_id: str):
    get_account(account_id)
    with manager.lock, store.db() as conn:
        if conn.execute("SELECT id FROM tasks WHERE account_id=? AND status IN ('queued','running','paused','cancelling','waiting_answer','retry_wait')", (account_id,)).fetchone():
            raise HTTPException(409, "请先停止此账号的所有任务")
        task_ids = [r["id"] for r in conn.execute("SELECT id FROM tasks WHERE account_id=?", (account_id,))]
        course_ids = [r["id"] for r in conn.execute("SELECT id FROM courses WHERE account_id=?", (account_id,))]
        manager.cancel_account_progress(account_id)
        conn.execute("DELETE FROM tasks WHERE account_id=?", (account_id,))
        conn.execute("DELETE FROM accounts WHERE id=?", (account_id,))
    import shutil
    for task_id in task_ids:
        shutil.rmtree(store.DATA / "jobs" / task_id, ignore_errors=True)
    for course_id in course_ids:
        (store.DATA / "quiz-state" / (course_id + ".json")).unlink(missing_ok=True)
        (store.DATA / 'quiz-state' / (course_id + '.lock')).unlink(missing_ok=True)
        point_file = store.DATA / 'point-state' / (course_id + '.sqlite')
        point_file.unlink(missing_ok=True)
        for suffix in ('-wal', '-shm', '-journal'):
            point_file.with_name(point_file.name + suffix).unlink(missing_ok=True)
        shutil.rmtree(point_file.with_suffix('.locks'), ignore_errors=True)
    return {"ok": True}


@app.post("/api/accounts/{account_id}/sync")
def sync_courses(account_id: str):
    account = get_account(account_id)
    payload = {"action": "scan", "mode": account["mode"], "credentials": store.unseal(account["credentials"])}
    with tempfile.TemporaryDirectory(prefix="study-scan-") as directory:
        try:
            result = subprocess.run(worker_command('--study-scan-id=' + account_id),
                input=json.dumps(payload), text=True, encoding="utf-8", capture_output=True, cwd=directory, timeout=100)
        except subprocess.TimeoutExpired:
            raise HTTPException(504, "同步超时，请检查这台电脑 到学习通的网络")
        if result.returncode:
            state = store.read_json(__import__("pathlib").Path(directory) / "state.json", {})
            store.execute("UPDATE accounts SET status='attention' WHERE id=?", (account_id,))
            raise HTTPException(400, state.get("error", "读取课程失败，请检查账号或平台接口"))
        try:
            courses = json.loads(result.stdout)["courses"]
        except (ValueError, KeyError):
            raise HTTPException(502, "课程接口返回内容异常")
    with store.db() as conn:
        for position, course in enumerate(courses):
            key = f'{account_id}:{course["courseId"]}:{course["clazzId"]}:{course.get("cpi", "")}'
            course_id = hashlib.sha256(key.encode()).hexdigest()[:24]
            conn.execute('''INSERT INTO courses (id,account_id,title,teacher,raw,total,done,synced_at,source_position)
                VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                title=excluded.title,teacher=excluded.teacher,raw=excluded.raw,synced_at=excluded.synced_at,source_position=excluded.source_position''',
                (course_id, account_id, course["title"], course.get("teacher", ""), json.dumps(course, ensure_ascii=False),
                 course.get("total", 0), course.get("done", 0), store.now(), position))
        conn.execute("UPDATE accounts SET status='ready' WHERE id=?", (account_id,))
    return {"count": len(courses)}


@app.post("/api/demo", status_code=201)
def demo(new_account: bool = False):
    existing = store.one("SELECT id FROM accounts WHERE mode='demo' ORDER BY created_at")
    if existing and not new_account:
        return {"account_id": existing["id"]}
    account_id = uuid.uuid4().hex
    count = len(store.rows("SELECT id FROM accounts WHERE mode='demo'"))
    store.execute("INSERT INTO accounts VALUES (?,?,?,?,?,?,?,?)", (account_id, f"体验账号 {count + 1}", "演示数据 · 不连接学习通",
        "demo:" + account_id, store.seal({}), "demo", "ready", store.now()))
    sync_courses(account_id)
    return {"account_id": account_id}


class TaskBody(BaseModel):
    course_ids: list[str] = Field(min_length=1, max_length=20)
    engine: str = "server"
    point_concurrency: int | None = Field(default=None, ge=1, le=MAX_POINT_CONCURRENCY)
    task_point_limit: int | None = Field(default=None, ge=1, le=100)


@app.post("/api/tasks", status_code=201)
def create_tasks(body: TaskBody):
    if body.engine != "server":
        raise HTTPException(400, "本地版仅支持在这台电脑 上执行任务")
    created = []
    with manager.lock, store.db() as conn:
        for course_id in dict.fromkeys(body.course_ids):
            course = conn.execute("SELECT * FROM courses WHERE id=?", (course_id,)).fetchone()
            if not course:
                raise HTTPException(404, "所选课程不存在，请刷新列表")
            if conn.execute("SELECT id FROM tasks WHERE course_id=? AND status IN ('queued','running','paused','cancelling','waiting_answer','retry_wait')", (course_id,)).fetchone():
                raise HTTPException(409, "所选课程中有正在运行的任务，请勿重复启动")
            task_id = uuid.uuid4().hex
            state = {"status": "queued", "progress": 0, "chapter": "等待运行", "logs": [],
                     'point_concurrency': body.point_concurrency or store.setting('point_concurrency', 1),
                     'task_point_limit': body.task_point_limit}
            title = course['title'] + (f' · 最多 {body.task_point_limit} 个任务点' if body.task_point_limit else '')
            conn.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?)", (task_id, course["account_id"], course_id,
                title, body.engine, "queued", 0, json.dumps(state), store.now(), store.now()))
            created.append(task_id)
    return {"task_ids": created}


class ControlBody(BaseModel):
    action: str


@app.post('/api/tasks/{task_id}/retry-now', status_code=202)
def retry_task_now(task_id: str):
    with manager.lock:
        task = get_task(task_id)
        if task['engine'] != 'server' or task['status'] != 'retry_wait':
            raise HTTPException(409, '只有等待自动重试的本机任务可以立即重试')
        if task_id in manager.processes:
            raise HTTPException(409, '上一轮任务正在收尾，请稍后再次点击')
        state = json.loads(task['state'])
        point_store = store.DATA / 'point-state' / (task['course_id'] + '.sqlite')
        if point_store.exists():
            from app.point_queue import PointLedger
            PointLedger(point_store).reset_recoveries()
        state.update(status='queued', next_retry_at=None, error=None, chapter='已请求立即重试，等待运行位置')
        logs = state.setdefault('logs', [])
        logs.append({'time': store.now(), 'level': 'info', 'message': '已手动请求立即重试，保留进度和交卷核验记录'})
        state['logs'] = logs[-200:]
        update_task(task_id, state)
    return {'ok': True, 'task_id': task_id, 'status': 'queued'}


@app.post("/api/tasks/{task_id}/control")
def control_task(task_id: str, body: ControlBody):
    if body.action not in ("pause", "run", "cancel"):
        raise HTTPException(400, "未知操作")
    with manager.lock:
        manager.control(get_task(task_id), body.action)
    return {"ok": True}


class SettingsBody(BaseModel):
    concurrency: int = Field(ge=1, le=8)
    point_concurrency: int | None = Field(default=None, ge=1, le=MAX_POINT_CONCURRENCY)


class PointConcurrencyBody(BaseModel):
    concurrency: int = Field(ge=1, le=MAX_POINT_CONCURRENCY)


def set_point_concurrency(task, count):
    path = store.DATA / 'jobs' / task['id']
    path.mkdir(parents=True, exist_ok=True)
    store.write_json(path / 'runtime.json', {'point_concurrency': count})
    state = json.loads(task['state'])
    state['point_concurrency'] = count
    update_task(task['id'], state)


@app.put('/api/tasks/{task_id}/point-concurrency')
def task_point_concurrency(task_id: str, body: PointConcurrencyBody):
    with manager.lock:
        task = get_task(task_id)
        if task['status'] not in ('queued','running','paused','waiting_answer','retry_wait') or task['engine'] != 'server':
            raise HTTPException(409, '只有本机进行中的任务可以调整并发')
        set_point_concurrency(task, body.concurrency)
    return {'ok': True, 'point_concurrency': body.concurrency}


class AnswersBody(BaseModel):
    quiz_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    answers: dict[str, str | list[str] | bool]


@app.post("/api/tasks/{task_id}/answers", status_code=202)
def answer_task(task_id: str, body: AnswersBody):
    from app.quiz_codec import canonical_answer
    with manager.lock:
        task = get_task(task_id)
        pending = json.loads(task["state"]).get("pending_quiz")
        if task["status"] != "waiting_answer" or not pending or pending["id"] != body.quiz_id:
            raise HTTPException(409, "此题目已处理或任务状态已变化，请刷新详情")
        values = {}
        for question in pending["questions"]:
            value = body.answers.get(question["id"], "")
            if len(json.dumps(value, ensure_ascii=False)) > 10000:
                raise HTTPException(400, "答案过长")
            values[question["id"]] = canonical_answer(question, value)
        path = store.DATA / "jobs" / task_id / f"answers-{body.quiz_id}.json"
        if pending.get('unit_id'):
            unit_id = pending['unit_id']
            if not re.fullmatch(r'[0-9a-f]{24}', unit_id):
                raise HTTPException(409, '任务点编号无效')
            path = store.DATA / 'jobs' / task_id / 'units' / unit_id / f'answers-{body.quiz_id}.json'
            unit_state = store.read_json(path.parent / 'state.json', {})
            if unit_state.get('status') != 'waiting_answer' or (unit_state.get('pending_quiz') or {}).get('id') != body.quiz_id:
                raise HTTPException(409, '此任务点的题目已变化，请刷新详情')
        if path.exists():
            raise HTTPException(409, "答案正在提交，请勿重复操作")
        store.write_json(path, {"answers": values})
    return {"ok": True}


class QuizBody(BaseModel):
    provider: str
    endpoint: str = Field(default="", max_length=500)
    model: str = Field(default="", max_length=150)
    key: str = Field(default="", max_length=500)
    tokens: str = Field(default="", max_length=1000)
    go_authorization: str = Field(default="", max_length=500)
    mode: str = "unattended"
    auto_ai_fallback: bool = True
    ai_vision: bool = True
    fallbacks: list[str] = Field(default_factory=lambda: ["TikuGo"], max_length=6)
    attempts: int = Field(default=1, ge=1, le=3)
    max_retries: int = Field(default=6, ge=1, le=20)
    retry_delay: int = Field(default=60, ge=1, le=900)
    url: str = Field(default="", max_length=500)
    like_tokens: str = Field(default="", max_length=1000)
    likeapi_model: str = Field(default="", max_length=150)
    likeapi_vision: bool = True
    siliconflow_key: str = Field(default="", max_length=500)
    siliconflow_model: str = Field(default="deepseek-ai/DeepSeek-V3", max_length=150)
    siliconflow_endpoint: str = Field(default="https://api.siliconflow.cn/v1/chat/completions", max_length=500)


def validated_quiz_config(body: QuizBody):
    from app.quiz import DEFAULT_CONFIG, PROVIDERS, SECRET_KEYS, source_available
    if body.provider not in PROVIDERS:
        raise HTTPException(400, "未知的答题服务")
    config = {**DEFAULT_CONFIG, **store.setting("quiz", {}), **body.model_dump(exclude=set(SECRET_KEYS))}
    for key in SECRET_KEYS:
        if getattr(body, key).strip():
            config[key] = getattr(body, key).strip()
    if body.mode not in ("unattended", "assisted") or any(name not in PROVIDERS or name == "manual" for name in body.fallbacks):
        raise HTTPException(400, "无效的运行模式或备用来源")
    selected = list(dict.fromkeys([body.provider, *body.fallbacks]))
    for address in ("endpoint", "url", "siliconflow_endpoint"):
        if not config[address]:
            continue
        endpoint = urlparse(config[address])
        if not endpoint.hostname or (endpoint.scheme != "https" and not (endpoint.scheme == "http" and endpoint.hostname in ("localhost", "127.0.0.1", "::1"))):
            raise HTTPException(400, "AI 地址请使用 HTTPS，或本机 HTTP 地址")
    if body.provider == "AI":
        if not source_available("AI", config):
            raise HTTPException(400, "请填写 AI 模型与 API Key")
    if body.provider == "TikuYanxi" and not config["tokens"]:
        raise HTTPException(400, "言溪题库需要 Token")
    if body.provider in ("TikuLike", "TikuAdapter", "SiliconFlow") and not source_available(body.provider, config):
        raise HTTPException(400, "请填写所选来源的凭据或地址")
    if body.mode == "unattended" and not any(source_available(name, config) for name in selected):
        raise HTTPException(400, "无人值守模式需要至少一个可用答题来源")
    return config


@app.put("/api/quiz")
def quiz_settings(body: QuizBody):
    config = validated_quiz_config(body)
    with manager.lock:
        store.set_setting("quiz", config)
        for task in store.rows("SELECT * FROM tasks WHERE status='retry_wait' AND engine='server'"):
            state = json.loads(task["state"])
            state.update(next_retry_at=store.now(), chapter="答题设置已更新，等待重新运行")
            update_task(task["id"], state)
    return {"ok": True}


@app.post("/api/quiz/test")
def test_quiz_connection(body: QuizBody):
    from app.quiz import query_ai, source_available
    from app.quiz_codec import canonical_answer
    config = validated_quiz_config(body)
    if not source_available("AI", config):
        raise HTTPException(400, "请先填写 AI 地址、模型；云端服务还需要 API Key，本机服务可留空")
    question = {"type": "single", "title": "连接测试：1 + 1 等于几？", "options": "A. 1\nB. 2\nC. 3"}
    started = time.monotonic()
    try:
        answer = canonical_answer(question, query_ai("AI", question, config))
        if answer != "B":
            raise ValueError("连接测试答案错误")
    except Exception:
        raise HTTPException(502, "AI 测试未通过：请检查地址、模型、凭据，以及是否支持有效 JSON 正文；未发送真实题目") from None
    return {"ok": True, "latency_ms": round((time.monotonic() - started) * 1000), "message": "AI 连接与答案格式测试通过；点击保存启用兜底"}


@app.put("/api/settings")
def settings(body: SettingsBody):
    with manager.lock:
        store.set_setting("concurrency", body.concurrency)
        if body.point_concurrency is not None:
            store.set_setting('point_concurrency', body.point_concurrency)
            for task in store.rows("SELECT * FROM tasks WHERE engine='server' AND status IN ('queued','running','paused','waiting_answer','retry_wait')"):
                set_point_concurrency(task, body.point_concurrency)
    return {"ok": True}


@app.get("/api/updates/check")
def check_updates():
    import requests
    try:
        release = updater.check_release()
    except requests.RequestException:
        raise HTTPException(502, "无法连接 GitHub，请稍后重试") from None
    result = {key: release.get(key) for key in ("current_version", "latest_version", "update_available", "asset_available")}
    tag = result["latest_version"]
    if tag:
        base = f"https://github.com/{release['repository']}/releases"
        result["release_url"] = f"{base}/tag/{tag}"
        download = f"{base}/download/{tag}/{updater.ASSET_NAME}"
        if release.get("download_url") == download:
            result["download_url"] = download
    return result


class UpdateBody(BaseModel):
    version: str = Field(min_length=1, max_length=32)


@app.post("/api/updates/install")
def install_update(body: UpdateBody, background: BackgroundTasks):
    if not updater.portable() or not hasattr(app.state, "restart"):
        raise HTTPException(400, "一键更新仅适用于 Windows 绿色版")
    with manager.lock:
        if manager.updating or mutations or manager.processes or manager.progress_processes or store.one(
            "SELECT id FROM tasks WHERE status IN ('running','queued','paused','retry_wait','waiting_answer','cancelling') LIMIT 1"
        ) or store.one("SELECT id FROM courses WHERE progress_status IN ('queued','running') LIMIT 1"):
            raise HTTPException(409, "有未结束任务、进度刷新或其他操作，请等结束后再更新")
        manager.updating = True
    try:
        release = updater.check_release()
        if body.version != release.get("latest_version"):
            raise ValueError("最新版本已变化，请重新检查更新")
        key = updater.prepare(release)
        updater.activate(key)
        background.add_task(app.state.restart)
        return {"restarting": True, "version": body.version}
    except Exception as error:
        manager.updating = False
        if isinstance(error, ValueError):
            raise
        raise HTTPException(502, "下载、校验或试启动失败，仍在使用当前版本") from None
