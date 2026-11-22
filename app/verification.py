"""Distinguish an active verification page from dormant quiz-page widgets."""
import re

from bs4 import BeautifulSoup


def requires_verification(response):
    if "text/html" not in response.headers.get("Content-Type", ""):
        return False
    if not any(marker in response.text for marker in ("processVerifyPng", "请输入验证码", "请完成验证", "workVerifyImg")):
        return False
    soup = BeautifulSoup(response.text, "lxml")
    for node in soup.select("script,style,template"):
        node.decompose()

    def visible(node):
        for ancestor in [node, *node.parents]:
            if not getattr(ancestor, "attrs", None):
                continue
            style = re.sub(r"\s+", "", ancestor.get("style", "")).lower()
            if ancestor.has_attr("hidden") or ancestor.get("aria-hidden") == "true" or "display:none" in style or "visibility:hidden" in style:
                return False
            if set(ancestor.get("class", [])) & {"hidden", "hide", "d-none"}:
                return False
        return True

    for text in soup.find_all(string=lambda value: value and any(marker in value for marker in ("请输入验证码", "请完成验证"))):
        if visible(text.parent):
            return True
    for image in soup.select('img[src*="processVerifyPng"],img[id="workVerifyImg"],img[src*="captcha"]'):
        if visible(image):
            return True
    return False
