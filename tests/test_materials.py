import base64
import io
import zipfile
from unittest.mock import Mock

import pytest
import requests

from app import materials, quiz, worker
from app.quiz_codec import decode_work_page, encode_form
from app.retry import WorkUnavailable

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aG1sAAAAASUVORK5CYII=')


def document():
    stream=io.BytesIO()
    with zipfile.ZipFile(stream,'w') as archive:
        archive.writestr('word/document.xml','<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>题目一：计算两个数的和</w:t></w:r></w:p><w:p><w:r><w:t>已知数据为1和2</w:t></w:r></w:p></w:body></w:document>')
        archive.writestr('word/media/figure.png',PNG)
    return stream.getvalue()


def test_word_text_and_embedded_figures_are_preserved():
    text,images=materials.document_parts(document())
    assert text=='题目一：计算两个数的和\n已知数据为1和2'
    assert len(images)==1 and images[0].startswith('data:image/png;base64,')
    with pytest.raises(WorkUnavailable):materials.document_parts(b'<html>login</html>')


def test_download_rejects_bad_transport_and_redirects():
    session=Mock()
    with pytest.raises(WorkUnavailable,match='HTTPS'):
        materials.fetch_material(session,'http://ptr.fy.chaoxing.com/file','https://mooc1.chaoxing.com/work')
    session.get.assert_not_called()
    session.get.return_value.status_code=302
    session.get.return_value.url='https://p.ananas.chaoxing.com/file'
    session.get.return_value.headers={'Location':'https://example.invalid/collect'}
    with pytest.raises(WorkUnavailable,match='重定向'):
        materials.fetch_material(session,'https://p.ananas.chaoxing.com/file','https://mooc1.chaoxing.com/work')
    assert session.get.call_count==1
    session.get.side_effect=requests.exceptions.SSLError('CERTIFICATE_VERIFY_FAILED: hostname mismatch')
    with pytest.raises(WorkUnavailable,match='证书'):
        materials.fetch_material(session,'https://p.ananas.chaoxing.com/file','https://mooc1.chaoxing.com/work')


def test_embedded_image_needs_no_network_and_preserves_bytes():
    session=Mock()
    assert materials.fetch_material(session,materials.image_uri(PNG),'https://mooc1.chaoxing.com')==PNG
    session.get.assert_not_called()


def test_transparent_black_formula_is_sent_on_an_explicit_white_canvas():
    from PIL import Image
    source = Image.new('RGBA', (2, 1), (0, 0, 0, 0))
    source.putpixel((0, 0), (0, 0, 0, 255))
    buffer = io.BytesIO(); source.save(buffer, format='PNG')
    uri = materials.image_uri(buffer.getvalue())
    result = Image.open(io.BytesIO(base64.b64decode(uri.split(',', 1)[1])))
    assert result.mode == 'RGB'
    assert result.getpixel((0, 0)) == (0, 0, 0) and result.getpixel((1, 0)) == (255, 255, 255)


def test_actual_other_attachment_form_requires_material_and_keeps_raw_type():
    html='''<form><input name="workId" value="w"><div class="singleQuesId" data="1"><div class="TiMu" data="8"><div class="Zy_TItle">材料.doc <img src="/fileTypeImages/icon_doc.gif"><a href="https://p.ananas.chaoxing.com/file.doc">材料.doc</a></div><textarea name="answer1"></textarea><input type="hidden" name="answertype1" value="8"></div></div></form>'''
    q=decode_work_page(html)['questions'][0]
    assert q['type']=='shortanswer' and q['raw_type']=='8' and q['material_required']
    assert not q['image_sources'] and q['attachments']
    form=decode_work_page(html)
    data=encode_form(form,['完整作答'])
    assert data['answertype1']=='8' and data['answer1']=='完整作答'


def test_material_preparation_has_text_and_labels_images(monkeypatch):
    monkeypatch.setattr(materials,'fetch_material',lambda *_:document())
    question={'material_required':True,'attachments':[{'url':'https://p.ananas.chaoxing.com/file.docx'}]}
    materials.prepare_questions([question],Mock(),'https://mooc1.chaoxing.com',{},Mock())
    assert '已知数据' in question['material_text']
    assert question['image_data'][0]['label']=='附件图片 1'


def test_ai_receives_actual_image_bytes_and_material_not_only_urls(monkeypatch):
    response=Mock()
    response.json.return_value={'choices':[{'finish_reason':'stop','message':{'content':'{"Answer":"完整作答"}'}}]}
    post=Mock(return_value=response)
    monkeypatch.setattr(requests,'post',post)
    cfg={**quiz.DEFAULT_CONFIG,'endpoint':'https://example.invalid/v1','key':'secret','model':'vision'}
    q={'type':'shortanswer','title':'材料.doc','material_text':'具体的题面内容','image_data':[{'label':'选项 A 图片','url':materials.image_uri(PNG)}]}
    assert quiz.query_ai('AI',q,cfg)=='完整作答'
    content=post.call_args.kwargs['json']['messages'][1]['content']
    assert '具体的题面内容' in content[0]['text']
    assert content[2]['image_url']['url'].startswith('data:image/png;base64,')


def test_material_filename_alone_never_reaches_bank_or_ai(tmp_path,monkeypatch):
    monkeypatch.chdir(tmp_path)
    source=Mock();ai=Mock()
    monkeypatch.setattr(quiz,'make_source',source);monkeypatch.setattr(quiz,'query_ai',ai)
    cfg={'endpoint':'https://example.invalid/v1','key':'secret','model':'test'}
    q={'id':'1','type':'shortanswer','title':'材料.doc','material_required':True}
    from app.retry import RetryNeeded
    with pytest.raises(RetryNeeded):quiz.build_tiku(cfg,worker.Reporter({}),worker.NeedsAttention).resolve([q])
    source.assert_not_called();ai.assert_not_called()


def test_course_image_cdn_downloads_without_platform_cookies(monkeypatch):
    response = Mock(status_code=200)
    response.iter_content.return_value = [PNG]
    get = Mock(return_value=response)
    monkeypatch.setattr(requests, 'get', get)
    session = Mock()
    session.headers = {'User-Agent': 'Mozilla/5.0', 'Authorization': 'private'}
    assert materials.fetch_material(session, 'https://p.cldisk.com/star3/image.png', 'https://mooc1.chaoxing.com/work') == PNG
    session.get.assert_not_called()
    assert get.call_count == 1 and 'cookies' not in get.call_args.kwargs and 'auth' not in get.call_args.kwargs
    assert get.call_args.kwargs['headers'] == {'User-Agent': 'Mozilla/5.0', 'Accept': 'image/*'}
    for url in ('http://p.cldisk.com/image.png', 'https://p.cldisk.com.evil.invalid/image.png', 'https://evil.cldisk.com/image.png'):
        with pytest.raises(WorkUnavailable):
            materials.fetch_material(session, url, 'https://mooc1.chaoxing.com/work')
    assert get.call_count == 1
