from types import SimpleNamespace

from app.verification import requires_verification


def response(html):
    return SimpleNamespace(text=html, headers={"Content-Type": "text/html;charset=utf-8"})


def test_hidden_quiz_verification_dialog_is_not_an_active_challenge():
    html = '''<form><div class="singleQuesId">题目</div></form>
    <div id="verifyCodeWin" style="width:338px;display:none;">
    请输入验证码<img id="workVerifyImg" src="/processVerifyPng.ac"></div>'''
    assert not requires_verification(response(html))
    assert requires_verification(response(html.replace("display:none;", "display:block;")))


def test_script_template_and_hidden_ancestor_do_not_trigger_verification():
    assert not requires_verification(response('<script>const tip="请完成验证";</script><div>题目</div>'))
    assert not requires_verification(response('<div hidden><p>请输入验证码</p></div>'))
    assert not requires_verification(response('<div class="hidden"><img id="workVerifyImg"></div>'))


def test_real_verification_page_still_blocks_execution():
    assert requires_verification(response('<h1>请完成验证</h1><input name="code">'))
    assert requires_verification(response('<img src="/processVerifyPng.ac"><input name="code">'))


def test_ai_auth_failure_is_not_misclassified_as_learning_login_expired(tmp_path,monkeypatch):
    from unittest.mock import Mock
    import requests
    from app import store,worker
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(store.ROOT/'vendor/chaoxing'))
    import api.base as base
    client=Mock()
    client.login.return_value={'status':True}
    monkeypatch.setattr(base,'Chaoxing',Mock(return_value=client))
    def reply(session,method,url,**kwargs):
        result=requests.Response();result.status_code=401;result.url=url
        result.headers['Content-Type']='application/json';result._content=b'{"error":"unauthorized"}'
        return result
    monkeypatch.setattr(requests.Session,'request',reply)
    payload={'action':'inspect','credentials':{'username':'test-user','password':'test-password'}}
    worker.platform_client(payload,worker.Reporter(payload))
    response=requests.get('https://ai.example.invalid/v1/chat/completions')
    assert response.status_code==401
    import pytest
    with pytest.raises(requests.HTTPError):response.raise_for_status()
    with pytest.raises(worker.NeedsAttention,match='登录过期'):
        requests.get('https://mooc1.chaoxing.com/mooc-ans/knowledge/cards')
