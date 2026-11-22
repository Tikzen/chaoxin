"""Bounded course-material input; never answer a document's filename alone."""
import base64
import io
import subprocess
import tempfile
import zipfile
from pathlib import Path
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree

import requests

from app.retry import RetryNeeded, WorkUnavailable

MAX_BYTES = 15_000_000
MAX_TEXT = 40000
MAX_IMAGES = 12


def supported_material_host(host):
    # cldisk is also served in authenticated course question pages. Keep the
    # extra CDN permission to this exact host rather than an entire domain.
    return bool(host and (host.endswith(('.chaoxing.com', '.chaoxing.com.cn', '.chaoxingcdn.com'))
                          or host == 'p.cldisk.com'))


def image_uri(data):
    mime = ("image/png" if data.startswith(b"\x89PNG") else "image/jpeg" if data.startswith(b"\xff\xd8")
            else "image/gif" if data.startswith(b"GIF8") else "image/webp" if data.startswith(b"RIFF") and data[8:12] == b"WEBP" else None)
    if not mime or len(data) > 5_000_000:
        raise WorkUnavailable("题目图片格式不支持或超过 5 MB，未发送不完整题面")
    from PIL import Image, UnidentifiedImageError
    try:
        with Image.open(io.BytesIO(data)) as source:
            if source.width * source.height > 20_000_000:
                raise WorkUnavailable('题目图片像素数量超限，未发送不完整题面')
            if 'A' in source.getbands() or 'transparency' in source.info:
                rgba = source.convert('RGBA')
                if rgba.getchannel('A').getextrema()[0] < 255:
                    # Formula PNGs often contain black ink on transparent
                    # pixels. Give vision services an explicit white canvas.
                    canvas = Image.new('RGBA', source.size, 'white')
                    canvas.alpha_composite(rgba)
                    output = io.BytesIO()
                    canvas.convert('RGB').save(output, format='PNG')
                    data, mime = output.getvalue(), 'image/png'
                    if len(data) > 5_000_000:
                        raise WorkUnavailable('转换后的题目图片超过 5 MB')
    except (OSError, UnidentifiedImageError, Image.DecompressionBombError):
        raise WorkUnavailable('题目图片无法完整解码，未提交不完整题面') from None
    return "data:" + mime + ";base64," + base64.b64encode(data).decode()


def fetch_material(session, url, page_url):
    if url.startswith("data:image/"):
        try:
            if len(url) > 7_000_000:
                raise ValueError()
            data = base64.b64decode(url.split(",", 1)[1], validate=True)
            image_uri(data)
            return data
        except (ValueError, IndexError):
            raise WorkUnavailable("内嵌图片数据无效或超限") from None
    target = urlparse(urljoin(page_url, url))
    if target.scheme != "https" or not supported_material_host(target.hostname):
        raise WorkUnavailable("题目材料链接不是已支持的 HTTPS 学习通来源，请更新课程材料链接")
    def download(address):
        # The external image CDN does not need the account's platform cookies.
        if urlparse(address).hostname == 'p.cldisk.com':
            headers = {'User-Agent': session.headers.get('User-Agent', 'Mozilla/5.0'), 'Accept': 'image/*'}
            return requests.get(address, headers=headers, timeout=(10, 30), stream=True, allow_redirects=False)
        return session.get(address, timeout=(10, 30), stream=True, allow_redirects=False)
    try:
        response = download(target.geturl())
        for _ in range(3):
            if response.status_code not in (301, 302, 303, 307, 308):
                break
            destination = urlparse(urljoin(response.url, response.headers.get("Location", "")))
            response.close()
            if destination.scheme != "https" or not supported_material_host(destination.hostname):
                raise WorkUnavailable("材料下载重定向超出受支持的平台来源")
            response = download(destination.geturl())
        if response.status_code in (403, 429):
            response.close()
            raise RetryNeeded(f"材料下载临时受限（HTTP {response.status_code}），该任务点稍后自动恢复")
        if response.status_code in (404, 410):
            response.close()
            raise WorkUnavailable(f"平台材料无法下载（HTTP {response.status_code}），需要教师更新附件")
        response.raise_for_status()
        data = bytearray()
        try:
            for block in response.iter_content(65536):
                data.extend(block)
                if len(data) > MAX_BYTES:
                    raise WorkUnavailable("题目材料超过 15 MB，未截断题面后作答")
        finally:
            response.close()
        return bytes(data)
    except requests.exceptions.SSLError as error:
        # An EOF/handshake interruption is not a certificate-name failure.
        message = str(error).lower()
        if any(marker in message for marker in ('certificate_verify_failed', 'certificate verify failed', 'hostname mismatch', 'certificate is not valid', "doesn't match")):
            raise WorkUnavailable("材料链接证书校验失败，需要更新有效的 HTTPS 附件链接") from None
        raise RetryNeeded("材料 TLS 连接临时中断，该任务点稍后自动恢复；未关闭证书校验") from None
    except requests.RequestException:
        raise RetryNeeded("题目材料暂时无法下载，保留任务稍后重试；未凭文件名作答") from None


def document_parts(data):
    if data.startswith(b"\xd0\xcf\x11\xe0"):
        # macOS built-in converter parses legacy Word without opening Office UI.
        with tempfile.TemporaryDirectory(prefix="study-material-") as folder:
            source, converted = Path(folder) / "source.doc", Path(folder) / "source.docx"
            source.write_bytes(data)
            source.chmod(0o600)
            try:
                result = subprocess.run(["/usr/bin/textutil", "-convert", "docx", "-output", str(converted), str(source)],
                                        capture_output=True, timeout=30)
            except (OSError, subprocess.TimeoutExpired):
                raise WorkUnavailable("Word 材料转换不可用或超时，未发送不完整题面") from None
            if result.returncode or not converted.exists() or converted.stat().st_size > MAX_BYTES:
                raise WorkUnavailable("Word 材料转换失败，未凭附件名称作答")
            data = converted.read_bytes()
    if not data.startswith(b"PK\x03\x04"):
        raise WorkUnavailable("当前材料不是可解析的 Word 文档，需补充对应材料解析器")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            if sum(info.file_size for info in archive.infolist()) > 30_000_000:
                raise WorkUnavailable("Word 解压体积超限")
            tree = ElementTree.fromstring(archive.read("word/document.xml"))
            paragraphs = []
            for paragraph in tree.iter():
                if paragraph.tag.rsplit("}", 1)[-1] == "p":
                    text = "".join(node.text or "" for node in paragraph.iter() if node.tag.rsplit("}", 1)[-1] == "t")
                    if text.strip():
                        paragraphs.append(text.strip())
            text = "\n".join(paragraphs)
            images = [image_uri(archive.read(info)) for info in archive.infolist() if info.filename.startswith("word/media/") and not info.is_dir()]
    except (zipfile.BadZipFile, KeyError, ElementTree.ParseError):
        raise WorkUnavailable("Word 材料结构无效，未发送不完整题面") from None
    if len(text) > MAX_TEXT or len(images) > MAX_IMAGES:
        raise WorkUnavailable("Word 材料超过输入上限，未截断题面后作答")
    if not text and not images:
        raise WorkUnavailable("Word 附件未提取到题面")
    return text, images


def prepare_questions(questions, session, page_url, config, reporter):
    for question in questions:
        reporter.checkpoint()
        if len(question.get("attachments", [])) > 3 or len(question.get("image_sources", [])) > MAX_IMAGES:
            raise WorkUnavailable("题目材料数量超限，未截断题面后作答")
        image_data = []
        for attachment in question.get("attachments", []):
            reporter.log("正在读取题目附件材料")
            text, images = document_parts(fetch_material(session, attachment["url"], page_url))
            question["material_text"] = question.get("material_text", "") + "\n" + text
            image_data.extend({"label": f"附件图片 {index + 1}", "url": uri} for index, uri in enumerate(images))
            if len(image_data) > MAX_IMAGES:
                raise WorkUnavailable("题目图片数量超限，未截断题面后作答")
        if question.get("material_required") and not (question.get("material_text", "").strip() or image_data):
            raise WorkUnavailable("附件题缺少可读取的题面，未凭文件名作答")
        from app.quiz import source_available
        if config.get("ai_vision", True) and source_available("AI", config):
            for index, url in enumerate(question.get("image_sources", [])):
                label = next((f"选项 {choice['label']} 图片" for choice in question.get("choices", []) if url in choice.get("image_sources", [])), f"题面图片 {index + 1}")
                image_data.append({"label": label, "url": image_uri(fetch_material(session, url, page_url))})
        if len(image_data) > MAX_IMAGES:
            raise WorkUnavailable("题目图片数量超出上限，未截断题面后作答")
        if len(question.get("material_text", "")) > MAX_TEXT:
            raise WorkUnavailable("题目材料总长度超出上限，未截断题面后作答")
        if sum(len(image["url"]) for image in image_data) > 12_000_000:
            raise WorkUnavailable("题目图像总量超限，未截断题面后作答")
        question["image_data"] = image_data
