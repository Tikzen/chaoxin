"""Keep visible option labels separate from the platform's shuffled values."""
import hashlib
import json
import re
from urllib.parse import unquote, urlsplit, urlunsplit

from bs4 import BeautifulSoup


def math_page(html):
    soup = BeautifulSoup(html, 'lxml')
    for image in soup.select('img.ans-latex-moudle[data]'):
        try:
            value = json.loads(unquote(image['data']))
        except (ValueError, TypeError):
            continue
        if isinstance(value, str) and value.strip() and len(value) <= 4000:
            image.insert_before(' [公式: ' + value.strip() + '] ')
    return soup


def option_key(text, images):
    urls = []
    for image in images:
        target = urlsplit(image)
        urls.append(urlunsplit(target._replace(query='', fragment='')))
    content = {'text': re.sub(r'\s+', '', text), 'images': urls}
    return hashlib.sha256(json.dumps(content, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def read_choices(node, decode=lambda text: text, detail=False):
    result = []
    for index, item in enumerate(node.select('ul li')):
        marker = item.select_one('.num_option, .num_option_dx, [data-option], .option')
        if marker is None and detail:
            marker = item.select_one('i.fl')
        label = str(item.get('data-option') or (marker.get('data-option') or marker.get_text(strip=True) if marker else '')).strip('.．、)） ')
        if not re.fullmatch('[A-Z]', label):
            match = re.match(r'^([A-Z])(?:[.．、)）\s]|$)', str(item.get('aria-label') or item.get_text(' ', strip=True)))
            label = match[1] if match else chr(65 + index)
        value = str(marker.get('data', label)) if marker else label
        if not re.fullmatch('[A-Z]', value):
            raise ValueError('选项提交编码无效，拒绝猜测映射')
        content = item.find('a')
        if content is None:
            copy = BeautifulSoup(str(item), 'lxml')
            for unwanted in copy.select('label,input,button,script,style,.num_option,.num_option_dx,.option,i.fl'):
                unwanted.decompose()
            text = decode(copy.get_text(' ', strip=True))
            if marker is None:
                text = re.sub(rf'^{label}[.．、)）\s]+', '', text)
            if not text.strip():
                text = re.sub(r'^[A-Z][.．、)）\s]*', '', decode(str(item.get('aria-label', ''))))
        else:
            text = decode(content.get_text(' ', strip=True))
        text = re.sub(r'\s*选择\s*$', '', text).strip()
        images = list(dict.fromkeys(str(image['src']) for image in item.select('img[src]')))
        result.append({'label': label, 'text': text or (f'图片选项 {label}' if images else ''),
                       'submit_value': value, 'image_sources': images, 'content_key': option_key(text, images)})
    for key in ('label', 'submit_value'):
        if len({item[key] for item in result}) != len(result):
            raise ValueError('选项标签或提交编码重复，拒绝提交')
    return result


def feedback_answers(html, questions):
    """Use displayed corrections only when question IDs and all options match."""
    soup = math_page(html)
    nodes = {str(node.get('data')): node for node in soup.select('div.singleQuesId')}
    answers = {}
    for question in questions:
        node = nodes.get(str(question['id']))
        if node is None or question['type'] not in ('single', 'multiple'):
            continue
        correct = node.select_one('.correctAnswer .answerCon')
        if correct is None:
            continue
        prior = read_choices(node, detail=True)
        current = question.get('choices', [])
        if sorted(item['content_key'] for item in prior) != sorted(item['content_key'] for item in current):
            continue
        letters = re.sub(r'[\s,，、;；]+', '', correct.get_text(strip=True))
        if not re.fullmatch('[A-Z]+', letters) or (question['type'] == 'single' and len(letters) != 1):
            continue
        selected = []
        for letter in letters:
            old = [item for item in prior if item['label'] == letter]
            match = [item for item in current if old and item['content_key'] == old[0]['content_key']]
            if len(old) != 1 or len(match) != 1:
                break
            selected.append(match[0]['label'])
        else:
            answers[str(question['id'])] = selected[0] if question['type'] == 'single' else selected
    return answers
