"""Read course cards without invoking study methods or submitting learning records."""
import json
import re

TASK_TYPES = {"video": "音视频", "document": "文档", "read": "阅读", "workid": "章节测验", "live": "直播"}


def flag(value):
    return value is True or value == 1 or str(value).lower() == "true"


def is_empty_card_page(html):
    """Recognize the platform's explicit empty card, not a failed response."""
    if not re.search(r'\bmArg\s*=\s*(["\'])\1\s*;', html):
        return False
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, 'lxml')
    page = soup.select_one('#pageDiv.ans-cc')
    if page is None or page.select_one('iframe, object, embed, .ans-job-icon'):
        return False
    placeholder = page.select_one('.nullpage dd')
    return (placeholder is not None and placeholder.get_text(strip=True) == '暂无内容'
            and page.get_text(strip=True) == '暂无内容')


def parse_cards(html, chapter, completed_keys=None):
    if "章节未开放" in html:
        return [], False
    match = re.search(r"\bmArg\s*=\s*(\{.*?\})\s*;", html, re.S)
    if not match:
        # Only an explicit platform empty template proves there are no tasks.
        return [], is_empty_card_page(html)
    data = json.loads(match.group(1))
    if "attachments" not in data:
        return [], False
    result = []
    for card in data["attachments"]:
        prop = card.get("property") or {}
        kind = str(card.get("type", "unknown"))
        if kind not in TASK_TYPES and not flag(card.get('job')):
            continue
        job_id = card.get("jobid") or prop.get("_jobid")
        if not job_id and not flag(card.get("job")):
            continue
        key = str(job_id or card.get("id") or card.get("objectId") or "")
        if not key:
            continue
        passed = card.get("isPassed")
        completed = flag(passed) or bool(chapter.get("has_finished"))
        status = "completed" if completed else "pending" if passed is not None or flag(card.get('job')) else "unknown"
        if completed_keys:
            from app.point_queue import point_key
            if point_key(chapter, {'type': kind, 'jobid': key}) in completed_keys:
                status = 'completed'
        result.append({"id": key, "name": str(prop.get("name") or prop.get("title") or TASK_TYPES.get(kind, "任务点")),
            "type": kind, "status": status, "requires_answer": kind == "workid" and status != "completed"})
    return result, True


def inspect_course(client, course, session, reporter, completed_keys=None):
    points = client.get_course_point(course["courseId"], course["clazzId"], course["cpi"])["points"]
    if not points:
        raise ValueError("未读取到章节，无法确认进度；原有进度已保留")
    chapters = []
    for index, point in enumerate(points):
        reporter.checkpoint()
        reporter.update(chapter=f"正在读取任务点 {index + 1}/{len(points)}：{point['title']}")
        tasks, complete, empty_pages = {}, True, 0
        if point.get("need_unlock"):
            complete = False
        else:
            for page in range(7):
                reporter.checkpoint()
                client.rate_limiter.limit_rate()
                response = session.get("https://mooc1.chaoxing.com/mooc-ans/knowledge/cards", params={
                    "clazzid": course["clazzId"], "courseid": course["courseId"], "knowledgeid": point["id"],
                    "cpi": course["cpi"], "ut": "s", "num": str(page), "v": "2025-0424-1038-3", "mooc2": 1})
                response.raise_for_status()
                entries, valid = parse_cards(response.text, point, completed_keys)
                if not valid:
                    # Empty trailing cards are normal; missing first-card metadata is ambiguous.
                    if page == 0:
                        complete = False
                    break
                if is_empty_card_page(response.text):
                    empty_pages += 1
                for task in entries:
                    tasks[task["id"]] = task
                # Use the same bounded seven-card scan as upstream, without study_emptypage.
        explicit_empty = empty_pages == 7 and not tasks
        if not tasks and not explicit_empty and str(point.get("jobCount", "0")).isdigit() and int(point.get("jobCount", 0)) > 0:
            complete = False
        chapters.append({"id": str(point["id"]), "title": point["title"], "completed": bool(point.get("has_finished")),
            "locked": bool(point.get("need_unlock")), "tasks_loaded": complete, "empty": explicit_empty,
            "tasks": list(tasks.values())})
    all_tasks = [task for chapter in chapters for task in chapter["tasks"]]
    return {"chapters": chapters, "total": len(points), "done": sum(c["completed"] for c in chapters),
        "task_total": len(all_tasks), "task_done": sum(t["status"] == "completed" for t in all_tasks),
        "tasks_complete": all(c["tasks_loaded"] for c in chapters) and all(t["status"] != "unknown" for t in all_tasks)}


def demo_progress(course):
    chapters = []
    for index in range(course["total"]):
        completed = index < course["done"]
        kind = "workid" if index % 3 == 2 else "video"
        chapters.append({"id": str(index + 1), "title": f"第 {index + 1} 节 · 演示课程内容", "completed": completed,
            "locked": False, "tasks_loaded": True, "tasks": [{"id": f"demo-{index}", "name": TASK_TYPES[kind],
                "type": kind, "status": "completed" if completed else "pending", "requires_answer": kind == "workid" and not completed}]})
    return {"chapters": chapters, "total": course["total"], "done": course["done"],
            "task_total": course["total"], "task_done": course["done"], "tasks_complete": True}
