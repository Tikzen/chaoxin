"""Typed question parsing and form encoding, separate from answer generation."""
import json
import re
import sys
from html import unescape
from pathlib import Path
from urllib.parse import urljoin, urlparse, parse_qsl, urlencode, urlunparse

from bs4 import BeautifulSoup

TYPE_NAMES = {"single": "单选", "multiple": "多选", "completion": "填空", "judgement": "判断", "shortanswer": "简答"}


def plain(value):
    return BeautifulSoup(str(value), "html.parser").get_text(" ", strip=True)


def choices(question):
    if question.get("choices"):
        return question["choices"]
    result = []
    for line in str(question.get("options", "")).splitlines():
        line = line.strip()
        if not line:
            continue
        match = re.match(r"^([A-Z])(?:\s*[.．、:：)）]\s*|\s+|(?=[\u4e00-\u9fff]))(.*)$", line, re.S)
        if match:
            result.append({"label": match[1], "text": match[2].strip()})
    return result


def unwrap(value):
    if isinstance(value, dict):
        for key in ("Answer", "answer", "answers"):
            if key in value:
                return value[key]
        raise ValueError("答案对象缺少 Answer 字段")
    if isinstance(value, str):
        text = value.strip()
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I).strip()
        if text.startswith(("{", "[")):
            try:
                return unwrap(json.loads(text))
            except json.JSONDecodeError:
                raise ValueError("答案 JSON 不完整") from None
        return text
    return value


def canonical_answer(question, value):
    """Return letters / boolean / ordered blanks / text, never guessed content."""
    value = unwrap(value)
    if len(json.dumps(value, ensure_ascii=False)) > 20000:
        raise ValueError("答案长度异常")
    kind = question["type"]
    if value is None:
        raise ValueError("答案为空")
    if kind not in TYPE_NAMES:
        raise ValueError(f"未实现的题型代码：{question.get('raw_type', kind)}")
    if kind == "judgement":
        if isinstance(value, list):
            if len(value) != 1:
                raise ValueError("判断题只能有一个答案")
            value = value[0]
        if isinstance(value, bool):
            return value
        text = str(value).strip().casefold()
        if text in ("true", "t", "1", "正确", "对", "√", "是", "yes", "y"):
            return True
        if text in ("false", "f", "0", "错误", "错", "×", "否", "no", "n"):
            return False
        raise ValueError("判断题答案不是明确的是/否")
    if kind in ("single", "multiple"):
        options = choices(question)
        mapping = {option["label"]: option["text"] for option in options}
        if not mapping:
            raise ValueError("没有完整的选项标签")
        values = value if isinstance(value, list) else [value]
        letters = []
        for item in values:
            if not isinstance(item, str):
                raise ValueError("选项答案必须为文字或字母")
            text = item.strip()
            compact = re.sub(r"[\s,，;；、|]+", "", text.upper())
            if re.fullmatch(r"[A-Z]+", compact) and all(c in mapping for c in compact):
                letters.extend(compact)
                continue
            labeled = re.match(r"^([A-Z])[.．、:：)）\s]+(.+)$", text, re.S)
            def clean(s):
                return re.sub(r"\s+", "", unescape(plain(s))).casefold()
            if labeled and labeled[1] in mapping and clean(labeled[2]) == clean(mapping[labeled[1]]):
                letters.append(labeled[1])
                continue
            matches = [key for key, content in mapping.items() if clean(content) == clean(text)]
            if len(matches) != 1:
                # GO returns option text separated by #. Keep a literal delimiter
                # inside a whole matching option (e.g. C#) before trying splits.
                candidates = set()
                if kind == "multiple":
                    for delimiter in ("\n", "#", "|"):
                        parts = text.split(delimiter)
                        if len(parts) < 2:
                            continue
                        try:
                            candidates.add(tuple(canonical_answer(question, parts)))
                            # An explicit line boundary preserves a literal # in
                            # an option; only try the next format if it failed.
                            break
                        except ValueError:
                            pass
                if len(candidates) == 1:
                    letters.extend(next(iter(candidates)))
                    continue
                raise ValueError("答案不能唯一匹配选项")
            letters.append(matches[0])
        letters = sorted(set(letters))
        if not letters or (kind == "single" and len(letters) != 1):
            raise ValueError("选项数量与题型不符")
        return letters[0] if kind == "single" else letters
    if kind == "completion":
        count = int(question.get("blank_count", 1))
        if isinstance(value, str):
            if count > 1:
                variants = {tuple(parts) for delimiter in ("\n", "#", "；", ";")
                            if len(parts := value.split(delimiter)) == count and all(part.strip() for part in parts)}
                if len(variants) != 1:
                    raise ValueError(f"需要按顺序提供 {count} 个非空填空答案，分隔格式不明确")
                value = list(next(iter(variants)))
            else:
                value = [value]
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            raise ValueError("填空答案必须是有序文字数组")
        blanks = [item.strip() for item in value]
        if len(blanks) != count or not all(blanks):
            raise ValueError(f"需要按顺序提供 {count} 个非空填空答案")
        return blanks
    if isinstance(value, list):
        if len(value) != 1:
            raise ValueError("简答题需要一段完整答案")
        value = value[0]
    if not isinstance(value, str):
        raise ValueError("简答答案必须为完整文字")
    text = value.strip()
    if not text:
        raise ValueError("答案为空")
    return text


def provider_answer(question, canonical):
    """Compatibility text for upstream bank providers and the web form."""
    kind = question["type"]
    if kind in ("single", "multiple"):
        mapping = {item["label"]: item["text"] for item in choices(question)}
        letters = [canonical] if kind == "single" else canonical
        return "\n".join(f"{letter}. {mapping[letter]}" for letter in letters)
    if kind == "judgement":
        return "正确" if canonical else "错误"
    return "\n".join(canonical) if kind == "completion" else canonical


def submission_url(html, page_url="https://mooc1.chaoxing.com/mooc-ans/work/doHomeWorkNew"):
    soup = BeautifulSoup(html, "lxml")
    form = soup.find("form")
    action = form.get("action") if form else None
    if not action:
        return "https://mooc1.chaoxing.com/mooc-ans/work/addStudentWorkNew"
    target = urlparse(urljoin(page_url, action))
    if target.scheme != "https" or target.hostname not in ("mooc1.chaoxing.com", "mooc1-1.chaoxing.com") or target.path not in ("/mooc-ans/work/addStudentWorkNew", "/mooc-ans/work/addStudentWorkNewWeb"):
        raise ValueError("测验交卷地址不在已支持的平台范围内")
    # Match the actual page's version() function, preserving its signed query.
    params = dict(parse_qsl(target.query, keep_blank_values=True))
    params.update(ua="pc", formType="post", saveStatus="1", version=str(int(params.get("version", "0")) + 1))
    return urlunparse(target._replace(query=urlencode(params)))


def work_identifiers(html, fallback):
    soup = BeautifulSoup(html, "lxml")
    def value(name):
        field = soup.select_one(f'input[name="{name}"]')
        return str(field.get("value", "")) if field else ""
    return {"workId": value("workId") or value("workRelationId") or value("oldWorkId") or str(fallback),
            "workAnswerId": value("workAnswerId")}


def decode_work_page(html, page_url="https://mooc1.chaoxing.com/mooc-ans/work/doHomeWorkNew"):
    # Preserve the existing font decoding and hidden platform form tokens.
    vendor = str(Path(__file__).resolve().parents[1] / "vendor/chaoxing")
    if vendor not in sys.path:
        sys.path.insert(0, vendor)
    from api.logger import logger
    logger.remove()
    import api.cxsecret_font as fonts
    if not fonts.fonthash_dao.hash_map:
        fonts.fonthash_dao = fonts.FontHashDAO(str(Path(__file__).resolve().parents[1] / "vendor/chaoxing/resource/font_map_table.json"))
    from api.decode import decode_questions_info
    from app.question_dom import math_page, read_choices
    soup = math_page(html)
    enriched = str(soup)
    form = decode_questions_info(enriched)
    from api.decode import FontDecoder
    font_decoder = FontDecoder(enriched) if soup.find("style", id="cxSecretStyle") else None
    form["__submit_url"] = submission_url(html, page_url)
    # Metadata such as answerId is not an individual question's answer.
    for field in soup.select('form input[type="hidden"][name]'):
        name = str(field["name"])
        if name == "answerwqbid" or name.startswith("answertype") or re.fullmatch(r"answer\d+.*", name):
            continue
        form[name] = str(field.get("value", ""))
    nodes = {str(node.get("data", "")): node for node in soup.select("div.singleQuesId")}
    for question in form["questions"]:
        node = nodes.get(str(question["id"]))
        if node is None:
            raise ValueError("题目结构与题号不一致")
        question["raw_type"] = str(node.select_one("div.TiMu").get("data", ""))
        question["plain_title"] = plain(question["title"])
        question["image_sources"] = list(dict.fromkeys(str(img.get("src")) for img in node.select("img[src]")
            if img.get("src") and "/fileTypeImages/" not in str(img.get("src"))))
        links = node.select('.Zy_TItle a[href]')
        if question["raw_type"] == "8" and node.select_one(f'textarea[name="answer{question["id"]}"]') and links:
            # This actual platform form uses code 8 for a free-text response to
            # attached material. Keep raw type 8 in the POST; require its content.
            question.update(type="shortanswer", material_required=True,
                            attachments=[{"url": str(link["href"])} for link in links])
        options = read_choices(node, font_decoder.decode if font_decoder else lambda text: text) if question['type'] in ('single', 'multiple') else []
        question['choices'] = options or choices(question)
        if options:
            question['options'] = '\n'.join(f"{option['label']}. {option['text']}" for option in options)
        if question["type"] == "completion":
            prefix = f"answer{question['id']}"
            fields = []
            for field in node.select("input[name],textarea[name]"):
                name = str(field.get("name", ""))
                if (name.startswith(prefix) or re.fullmatch(rf"answerEditor{re.escape(str(question['id']))}\d+", name)) and field.get("type") != "hidden":
                    fields.append(name)
            size = node.select_one(f'input[name="tiankongsize{question["id"]}"]')
            declared = int(size.get("value", 0)) if size else 0
            if declared and declared != (len(fields) or 1):
                raise ValueError("填空声明数量与实际编辑器字段不一致")
            question["blank_fields"] = fields or [prefix]
            question["blank_count"] = len(fields) or 1
    hidden = soup.select_one('input[name="answerwqbid"]')
    if hidden and hidden.get("value"):
        expected = [value for value in str(hidden["value"]).split(",") if value]
        if expected != [str(q["id"]) for q in form["questions"]]:
            raise ValueError("页面题号清单与解析结果不一致，拒绝提交不完整题目")
    return form


def encode_form(form, answers):
    questions = form["questions"]
    if len(questions) != len(answers):
        raise ValueError("题目与答案数量不一致")
    result = {key: value for key, value in form.items() if key != "questions" and not key.startswith("__")}
    result["pyFlag"] = ""  # Never silently save an unattended task as a draft.
    for question, value in zip(questions, answers):
        answer = canonical_answer(question, value)
        qid = str(question["id"])
        result[f"answertype{qid}"] = question["answerField"][f"answertype{qid}"]
        if question["type"] == "completion":
            fields = question.get("blank_fields", [f"answer{qid}"])
            if len(fields) != len(answer):
                raise ValueError("填空数量与实际表单字段不一致")
            for field, text in zip(fields, answer):
                if field in result:
                    result[field] = [*result[field], text] if isinstance(result[field], list) else [result[field], text]
                else:
                    result[field] = text
        elif question["type"] == "judgement":
            result[f"answer{qid}"] = "true" if answer else "false"
        elif question["type"] in ("single", "multiple"):
            mapping = {item['label']: item.get('submit_value', item['label']) for item in choices(question)}
            selected = [answer] if question['type'] == 'single' else answer
            result[f"answer{qid}"] = ''.join(sorted(mapping[letter] for letter in selected))
        else:
            result[f"answer{qid}"] = answer
    return result
