"""Validated answer sources, bounded retries and explicit assisted mode."""
import copy
import json
import re
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

from app.quiz_codec import canonical_answer, choices, plain, provider_answer, TYPE_NAMES
from app.retry import RetryNeeded

PROVIDERS = ("TikuGo", "TikuYanxi", "TikuLike", "TikuAdapter", "AI", "SiliconFlow", "manual")
SECRET_KEYS = ("key", "tokens", "go_authorization", "like_tokens", "siliconflow_key")
PUBLIC_KEYS = ("provider", "endpoint", "model", "mode", "fallbacks", "attempts", "max_retries", "retry_delay", "url", "likeapi_model", "likeapi_vision", "siliconflow_model", "siliconflow_endpoint", "auto_ai_fallback", "ai_vision")
DEFAULT_CONFIG = {"provider": "TikuGo", "tokens": "", "go_authorization": "", "endpoint": "", "key": "", "model": "",
    "mode": "unattended", "fallbacks": ["TikuGo"], "auto_ai_fallback": True, "ai_vision": True, "attempts": 1, "max_retries": 6, "retry_delay": 60,
    "url": "", "like_tokens": "", "likeapi_model": "", "likeapi_vision": True,
    "siliconflow_key": "", "siliconflow_model": "deepseek-ai/DeepSeek-V3", "siliconflow_endpoint": "https://api.siliconflow.cn/v1/chat/completions"}

AI_BASE_TOKEN_BUDGET = 8192
AI_MAX_TOKEN_BUDGET = 32768


class AnswerTruncated(ValueError):
    pass


def normalize_answer(question, value):
    return provider_answer(question, canonical_answer(question, value))


def source_available(name, config):
    local = urlparse(config.get("endpoint", "")).hostname in ("localhost", "127.0.0.1", "::1")
    return {"TikuGo": True, "TikuYanxi": bool(config.get("tokens")), "TikuLike": bool(config.get("like_tokens")),
        "TikuAdapter": bool(config.get("url")), "AI": bool((config.get("key") or local) and config.get("endpoint") and config.get("model")),
        "SiliconFlow": bool(config.get("siliconflow_key")), "manual": False}.get(name, False)


def source_order(config):
    names = list(dict.fromkeys([config["provider"], *config["fallbacks"]]))
    if config.get("auto_ai_fallback", True):
        names.extend(name for name in ("AI", "SiliconFlow") if name not in names and source_available(name, config))
    return names


def query_ai(name, question, config, repair=False):
    """Do not flatten Answer arrays; a blank/boolean is meaningful structured data."""
    import requests
    silicon = name == "SiliconFlow"
    endpoint = config["siliconflow_endpoint"] if silicon else config["endpoint"].rstrip("/")
    if not endpoint.endswith("/chat/completions"):
        endpoint += "/chat/completions"
    model = config["siliconflow_model"] if silicon else config["model"]
    key = config["siliconflow_key"] if silicon else config["key"]
    prompt = {"question": plain(question["title"]), "type": question["type"], "options": choices(question),
              "blank_count": question.get("blank_count", 1), "material": question.get("material_text", "")}
    content = json.dumps(prompt, ensure_ascii=False)
    if question.get("image_data"):
        content = [{"type": "text", "text": content}]
        for item in question["image_data"]:
            content.extend([{"type": "text", "text": item["label"]}, {"type": "image_url", "image_url": {"url": item["url"]}}])
    budget = min(AI_MAX_TOKEN_BUDGET, max(1024, int(config.get('_ai_token_budget', AI_BASE_TOKEN_BUDGET))))
    timeout = min(180, max(90, budget / 128 + 30))
    response = requests.post(endpoint, headers={"Authorization": f"Bearer {key}"} if key else {}, timeout=(10, timeout), json={
        "model": model, "messages": [{"role": "system", "content":
            '只返回 JSON 对象 {"Answer": ...}，禁止解释、Markdown、思考过程。单选用选项字母字符串，多选用字母数组，判断用布尔值，填空用按空顺序的字符串数组，简答用字符串。无法解答返回 null。' +
            (' 上次未取得完整有效答案，请优先给出完整的最终 JSON，检查题型、空数和格式。' if repair else '')},
            {"role": "user", "content": content}], "max_tokens": budget})
    response.raise_for_status()
    data = response.json()
    result = data["choices"][0]
    if result.get('finish_reason') == 'length':
        raise AnswerTruncated(f'模型达到 {budget} token 输出预算后被截断，未取得完整答案')
    if result.get("finish_reason") not in (None, "stop"):
        raise ValueError("模型未正常结束或拒绝回答")
    content = result["message"].get("content")
    if not isinstance(content, str) or len(content) > 30000:
        raise ValueError("模型未返回有效正文")
    # Requiring an object prevents explanations being accepted as a short answer.
    text = content.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    result = json.loads(text)
    if not isinstance(result, dict) or "Answer" not in result or result["Answer"] is None:
        raise ValueError("模型缺少有效 Answer")
    return result["Answer"]


def query_yanxi_once(provider, question):
    """Rotate an exhausted token for the next call without recursively querying."""
    import requests
    response = requests.get(provider.api, params={'question': question['title'], 'token': provider.token}, timeout=(10, 25))
    response.raise_for_status()
    data = response.json()
    info = data.get('data') or {}
    if not data.get('code'):
        if provider._times == 0 or info.get('times') == 0 or '次数不足' in str(info.get('answer', '')):
            provider._token_index += 1
            provider.load_token()
        return None
    provider._times = info.get('times', provider._times)
    answer = info.get('answer')
    return answer.strip() if isinstance(answer, str) else answer


def make_source(name, config):
    from api.answer import Tiku
    provider_config = {**config, "provider": name, "submit": "true", "cover_rate": "1.0", "true_list": "正确,对,√,是",
        "false_list": "错误,错,×,否", "http_proxy": "", "min_interval_seconds": "1",
        "go_min_interval": "1", "go_retry_times": "1", "go_retry_backoff": "1",
        # LIKE's retry flag also guards the first request. Keep it enabled but
        # cap its inner loop at one; the resolver owns all configurable retries.
        "likeapi_retry": True, "likeapi_retry_times": 1,
        "tokens": config["like_tokens"] if name == "TikuLike" else config["tokens"]}
    provider = Tiku.get_tiku_from_config(provider_config)
    provider.init_tiku()
    if name == 'TikuYanxi':
        provider._query = lambda question: query_yanxi_once(provider, question)
    return provider


def build_tiku(config, reporter, attention_error):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "vendor" / "chaoxing"))
    from api.answer import Tiku
    import api.base as base
    from api.logger import logger
    logger.remove()
    config = {**DEFAULT_CONFIG, **config}
    sources = {}

    def prevent_random(*_args, **_kwargs):
        raise attention_error("答案未能匹配，已阻止随机作答")
    base.random_answer = prevent_random

    class WebTiku(Tiku):
        DISABLE = False
        SUBMIT = True
        COVER_RATE = 1.0
        is_manual = True

        def _query(self, _question):
            return None

        def resolve(self, questions):
            answers = [None] * len(questions)
            cache = None
            if reporter.payload.get('point_store'):
                try:
                    from app.answer_cache import AnswerCache
                    cache = AnswerCache(reporter.payload['point_store'], config)
                except Exception:
                    reporter.log('答案缓存暂不可用，本轮继续正常查询', 'warning')
            reporter.log(f"发现章节测验，共 {len(questions)} 题；正在验证答案格式")
            names = source_order(config)
            if config["provider"] == "manual" and config["mode"] == "assisted":
                names = []
            for index, question in enumerate(questions):
                truncations, feedback = 0, ''
                if question.get("type") not in TYPE_NAMES:
                    reporter.log(f"第 {index + 1} 题的题型代码 {question.get('raw_type', '未知')} 尚未适配，未猜测交卷格式", "warning")
                    continue
                if question.get("material_required") and not (question.get("material_text", "").strip() or question.get("image_data")):
                    reporter.log(f"第 {index + 1} 题的附件题面尚未读取，未凭文件名作答", "warning")
                    continue
                if cache is not None:
                    try:
                        saved = cache.get(question)
                        if saved is not None:
                            answers[index] = canonical_answer(question, saved)
                            reporter.update(answer_progress={'total': len(questions), 'matched': sum(a is not None for a in answers), 'source': '已验证答案缓存'})
                            reporter.log(f'第 {index + 1} 题复用已验证答案，不重复调用题库或 AI')
                            continue
                    except Exception:
                        pass
                    try:
                        truncations, feedback = cache.hint(question)
                    except Exception:
                        pass
                # Exhaust this question's source chain before starting the next
                # question. A failed lookup switches source immediately by default.
                for name in names:
                    if not source_available(name, config):
                        continue
                    # Text-only APIs cannot see an image merely because its URL is present.
                    has_images = bool(question.get("image_sources") or question.get("image_data"))
                    visual_source = (name == "TikuLike" and config["likeapi_vision"] and bool(question.get("image_sources")) and not question.get("material_required")) or (name == "AI" and config["ai_vision"] and bool(question.get("image_data")))
                    if has_images and not visual_source:
                        reporter.log(f"第 {index + 1} 题含图片，{name} 无图像输入，转向下一来源", "warning")
                        continue
                    if question.get("material_required") and name not in ("AI", "SiliconFlow"):
                        continue
                    for attempt in range(config["attempts"]):
                        reporter.checkpoint()
                        try:
                            if name in ("AI", "SiliconFlow"):
                                budget = min(AI_BASE_TOKEN_BUDGET * 2 ** min(truncations, 2), AI_MAX_TOKEN_BUDGET)
                                if truncations:
                                    reporter.log(f'第 {index + 1} 题使用 {budget} token 预算恢复截断响应')
                                raw = query_ai(name, question, {**config, '_ai_token_budget': budget}, repair=attempt > 0 or bool(feedback))
                            else:
                                if name not in sources:
                                    sources[name] = make_source(name, config)
                                # Bypass upstream's cache of unvalidated/flattened answers.
                                query = copy.deepcopy(question)
                                # The bank's direct query bypasses upstream query()'s
                                # cleanup; actual pages prefix titles with 1【单选题】.
                                query["title"] = re.sub(r"^\s*\d+\s*(?=【[^】]*题】)", "", query["title"])
                                query["title"] = re.sub(r"^【[^】]*题】\s*", "", query["title"]).strip()
                                if query.get("image_sources"):
                                    query["options"] = "\n".join(f"{c['label']}. {c['text']} " + " ".join(f'<img src="{url}">' for url in c.get("image_sources", [])) for c in choices(query))
                                raw = sources[name]._query(query)
                            if raw is None:
                                raise ValueError("未找到答案")
                            answers[index] = canonical_answer(question, raw)
                            if cache is not None:
                                try:
                                    cache.put(question, answers[index])
                                except Exception:
                                    reporter.log('本题答案缓存写入失败，继续当前任务', 'warning')
                            reporter.update(answer_progress={"total": len(questions), "matched": sum(answer is not None for answer in answers), "source": name})
                            reporter.log(f"第 {index + 1} 题：{name} 返回的答案已通过格式校验")
                            break
                        except attention_error:
                            raise
                        except Exception as error:
                            if name in ('AI', 'SiliconFlow'):
                                if isinstance(error, AnswerTruncated):
                                    truncations += 1
                                feedback = str(error)
                                if cache is not None:
                                    try:
                                        cache.note_failure(question, isinstance(error, AnswerTruncated), str(error))
                                    except Exception:
                                        pass
                                if isinstance(error, AnswerTruncated):
                                    reporter.log(f'第 {index + 1} 题 AI 输出被截断；后续恢复将提高该题预算，保留其他有效答案', 'warning')
                            switching = '，切换下一来源' if attempt + 1 == config['attempts'] else ''
                            reporter.log(f"第 {index + 1} 题：{name} 第 {attempt + 1}/{config['attempts']} 次未取得有效答案（{type(error).__name__}）{switching}", "warning")
                            if attempt + 1 < config["attempts"]:
                                reporter.checkpoint()
                                time.sleep(min(2 ** attempt, 8))
                    if answers[index] is not None:
                        break
            if any(answer is None for answer in answers):
                if config["mode"] != "assisted":
                    missing = sum(answer is None for answer in answers)
                    if config["auto_ai_fallback"] and not any(source_available(name, config) for name in ("AI", "SiliconFlow")):
                        reporter.log("AI 兜底尚未配置：请在运行设置填写 AI 地址、模型和凭据并测试连接；配置后等待重试的任务会重新排队", "warning")
                    raise RetryNeeded(f"{missing}/{len(questions)} 题暂未获得有效答案；未保存、未交卷，稍后自动重试")
                quiz_id = uuid.uuid4().hex
                chapter = reporter.state.get("chapter", "章节测验")
                public = [{key: q.get(key, 1 if key == "blank_count" else "") for key in ("id", "title", "options", "type", "blank_count")} for q in questions]
                pending = {"id": quiz_id, "questions": public, "answers": {
                    q["id"]: provider_answer(q, answer) if answer is not None else "" for q, answer in zip(questions, answers)}}
                reporter.update(status="waiting_answer", pending_quiz=pending, chapter=chapter + " · 等待答题")
                reporter.log("辅助模式：请在任务详情填写全部答案", "warning")
                answer_file = Path(f"answers-{quiz_id}.json")
                while True:
                    reporter.checkpoint()
                    if answer_file.exists():
                        try:
                            values = json.loads(answer_file.read_text(encoding="utf-8"))["answers"]
                            answers = [canonical_answer(q, values.get(str(q["id"]))) for q in questions]
                            answer_file.unlink()
                            break
                        except (ValueError, KeyError, OSError):
                            answer_file.unlink(missing_ok=True)
                            reporter.log("答案格式不完整，请重新填写", "warning")
                    time.sleep(0.5)
                reporter.update(status="running", pending_quiz=None, chapter=chapter + " · 正在提交测验")
            return answers

        def query_all(self, questions, query_delay=0):
            return [provider_answer(q, a) for q, a in zip(questions, self.resolve(questions))]

    return WebTiku()
