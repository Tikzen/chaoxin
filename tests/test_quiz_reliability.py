import json
from pathlib import Path
from unittest.mock import Mock

import pytest
import requests

from app import quiz, quiz_codec as codec, store, worker
from app.quiz_work import execute_work
from app.quiz_work import record_is_submitted as real_record_is_submitted
from app.retry import RetryNeeded, WorkUnavailable

HTML = (Path(__file__).parent / "fixtures/work-five-types.html").read_text()


@pytest.fixture(autouse=True)
def upstream(monkeypatch):
    monkeypatch.syspath_prepend(str(store.ROOT / "vendor/chaoxing"))
    from api.logger import logger
    logger.remove()


def test_actual_html_five_types_and_ordered_blank_encoding():
    form = codec.decode_work_page(HTML)
    assert [q['type'] for q in form['questions']] == ['single', 'multiple', 'completion', 'judgement', 'shortanswer']
    assert form['questions'][2]['blank_count'] == 2
    data = codec.encode_form(form, ['C++', ['C', 'A'], ['甲', '乙'], False, '完整说明'])
    assert data['answer1'] == 'A'
    assert data['answer2'] == 'AC'
    assert data['answer3_1'] == '甲' and data['answer3_2'] == '乙'
    assert 'answer3' not in data
    assert data['answer4'] == 'false'
    assert data['answer5'] == '完整说明'
    assert data['pyFlag'] == '' and data['enc'] == 'test-token'


def test_repeated_blank_fields_remain_separate():
    form = codec.decode_work_page(HTML.replace('answer3_1', 'answer3').replace('answer3_2', 'answer3'))
    data = codec.encode_form(form, ['A', 'AC', ['甲', '乙'], True, '说明'])
    assert data['answer3'] == ['甲', '乙']
    from requests import Request
    body = Request('POST', 'https://example.invalid', data=data).prepare().body
    assert body.count('answer3=') == 2


def test_image_only_choices_preserved_and_unknown_type_rejected():
    image = HTML.replace('aria-label="A. C++">A. C++', 'data-option="A"><img src="https://p.ananas.chaoxing.com/a.png">')
    question = codec.decode_work_page(image)['questions'][0]
    assert question['choices'][0]['label'] == 'A'
    assert question['choices'][0]['image_sources']
    form = codec.decode_work_page(HTML.replace('data="0"', 'data="99"'))
    with pytest.raises(ValueError, match='未实现'):
        codec.encode_form(form, ['A', 'AC', ['甲', '乙'], True, '说明'])
    with pytest.raises(ValueError, match='题号'):
        codec.decode_work_page(HTML.replace('1,2,3,4,5,', '1,2,3,4,5,6,'))


@pytest.mark.parametrize('value', [None, '', '{"Answer":', [], ['甲']])
def test_empty_and_malformed_multiblanks_rejected(value):
    with pytest.raises(ValueError):
        codec.canonical_answer({'type': 'completion', 'blank_count': 2}, value)


def test_provider_retries_and_falls_back_without_pause(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(quiz.time, 'sleep', lambda _: None)
    first = Mock()
    first._query.side_effect = ['Z', requests.Timeout()]
    second = Mock()
    second._query.return_value = 'B'
    monkeypatch.setattr(quiz, 'make_source', lambda name, _: first if name == 'TikuGo' else second)
    reporter = worker.Reporter({})
    provider = quiz.build_tiku({'provider': 'TikuGo', 'tokens': 'test', 'fallbacks': ['TikuYanxi'], 'attempts': 2}, reporter, worker.NeedsAttention)
    answers = provider.resolve([{'id': '1', 'type': 'single', 'title': '1+1', 'options': 'A. 1\nB. 2'}])
    assert answers == ['B'] and first._query.call_count == 2 and second._query.call_count == 1
    assert reporter.state['status'] == 'running'
    assert not reporter.state.get('pending_quiz')


def test_invalid_api_answers_defer_instead_of_waiting_or_saving(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(quiz.time, 'sleep', lambda _: None)
    source = Mock()
    source._query.return_value = None
    monkeypatch.setattr(quiz, 'make_source', lambda *_: source)
    reporter = worker.Reporter({})
    provider = quiz.build_tiku({}, reporter, worker.NeedsAttention)
    with pytest.raises(RetryNeeded):
        provider.resolve([{'id': '1', 'type': 'judgement', 'title': '测试'}])
    assert source._query.call_count == 1
    assert not reporter.state.get('pending_quiz')


def test_text_model_does_not_claim_it_can_read_image_urls(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    model = Mock()
    monkeypatch.setattr(quiz, 'query_ai', model)
    provider = quiz.build_tiku({'provider': 'AI', 'key': 'test', 'endpoint': 'https://example.invalid/v1', 'model': 'test', 'fallbacks': []}, worker.Reporter({}), worker.NeedsAttention)
    with pytest.raises(RetryNeeded):
        provider.resolve([{'type': 'single', 'image_sources': ['https://example.invalid/a.png']}])
    model.assert_not_called()


def test_ai_preserves_false_and_multiblank_json_and_rejects_truncation(monkeypatch):
    response = Mock()
    response.json.return_value = {'choices': [{'finish_reason': 'stop', 'message': {'content': '{"Answer":false}'}}]}
    monkeypatch.setattr(requests, 'post', lambda *_a, **_k: response)
    config = {**quiz.DEFAULT_CONFIG, 'key': 'secret', 'endpoint': 'https://example.invalid/v1'}
    q = {'type': 'judgement', 'title': '判断'}
    assert quiz.query_ai('AI', q, config) is False
    response.json.return_value['choices'][0]['message']['content'] = '{"Answer":["甲","乙"]}'
    assert quiz.query_ai('AI', q, config) == ['甲', '乙']
    response.json.return_value['choices'][0]['finish_reason'] = 'length'
    with pytest.raises(ValueError, match='截断'):
        quiz.query_ai('AI', q, config)


def response(text='', body=None):
    obj = Mock(text=text, status_code=200)
    obj.json.return_value = body or {'status': True, 'msg': '提交成功'}
    return obj


def setup_submission(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from api.base import SessionManager
    session = Mock()
    monkeypatch.setattr(SessionManager, 'get_session', lambda: session)
    # Existing flow tests use synthetic records declared to be formal.
    monkeypatch.setattr('app.quiz_work.record_is_submitted', lambda *_: True)
    client = Mock()
    client.tiku.resolve.return_value = ['A', 'AC', ['甲', '乙'], False, '说明']
    course = {'courseId': '1', 'clazzId': '2', 'cpi': '3'}
    job = {'jobid': 'work-w1', 'enc': 'token'}
    info = {'knowledgeid': 'k', 'ktoken': 't', 'cpi': '3'}
    return session, client, course, job, info, worker.Reporter({})


RECORD = '<span class="viewNum">第1次</span><span class="viewScore">80分</span>'


def test_submit_verifies_record_and_never_uses_draft(tmp_path, monkeypatch):
    session, client, *args = setup_submission(tmp_path, monkeypatch)
    session.get.side_effect = [response(HTML), response(''), response(RECORD)]
    session.post.return_value = response()
    assert execute_work(client, *args).is_success()
    assert session.post.call_args.kwargs['data']['pyFlag'] == ''
    assert args[-1].state['quiz_result'] == {'status': 'submitted', 'score': 80.0}
    # New worker / retry, same persistent journal: verify only, never resubmit.
    session.get.side_effect = None
    session.get.return_value = response(RECORD)
    assert execute_work(client, *args).is_success()
    assert session.post.call_count == 1


@pytest.mark.parametrize('failure', ['timeout', 'invalid_json', 'saved_only', 'no_record'])
def test_ambiguous_submit_is_never_repeated(tmp_path, monkeypatch, failure):
    session, client, *args = setup_submission(tmp_path, monkeypatch)
    session.get.side_effect = [response(HTML), response(''), response('')]
    if failure == 'timeout':
        session.post.side_effect = requests.Timeout()
    elif failure == 'invalid_json':
        session.post.return_value.json.side_effect = ValueError()
    else:
        session.post.return_value = response(body={'status': True, 'msg': '保存成功' if failure == 'saved_only' else '提交成功'})
    with pytest.raises(RetryNeeded):
        execute_work(client, *args)
    session.get.side_effect = None
    session.get.return_value = response('')
    with pytest.raises(RetryNeeded):
        execute_work(client, *args)
    assert session.post.call_count == 1
    # Later async server confirmation restores progress without another POST.
    session.get.return_value = response(RECORD)
    assert execute_work(client, *args).is_success()
    assert session.post.call_count == 1


def test_corrupt_journal_cannot_allow_another_submission(tmp_path, monkeypatch):
    session, client, *args = setup_submission(tmp_path, monkeypatch)
    (tmp_path / 'quiz-submissions.json').write_text('{broken')
    with pytest.raises(RetryNeeded):
        execute_work(client, *args)
    session.post.assert_not_called()


def test_retry_is_bounded_and_preserves_reason(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    reporter = worker.Reporter({})
    payload = {'quiz_config': {'max_retries': 2, 'retry_delay': 1}, 'previous_state': {'retry_count': 0}}
    worker.schedule_retry(payload, reporter, '无效 JSON')
    assert reporter.state['status'] == 'retry_wait' and reporter.state['next_retry_at']
    payload['previous_state']['retry_count'] = 2
    worker.schedule_retry(payload, reporter, '仍未取得答案')
    assert reporter.state['status'] == 'attention' and reporter.state['next_retry_at'] is None


@pytest.mark.parametrize("kind,value", [("shortanswer", {"Answer": {"error":"bad"}}), ("completion", [{"error":"bad"}]), ("single", [False])])
def test_structurally_wrong_model_values_are_rejected(kind, value):
    with pytest.raises(ValueError):
        codec.canonical_answer({"type": kind, "options": "A. one\nB. two"}, value)


def test_invalid_answers_never_create_submission_intent(tmp_path, monkeypatch):
    session, client, *args = setup_submission(tmp_path, monkeypatch)
    session.get.side_effect = [response(HTML), response('')]
    client.tiku.resolve.side_effect = RetryNeeded("无有效答案")
    with pytest.raises(RetryNeeded):
        execute_work(client, *args)
    session.post.assert_not_called()
    assert not (tmp_path / 'quiz-submissions.json').exists()


def test_explicit_rejection_allows_fresh_form_retry(tmp_path, monkeypatch):
    session, client, *args = setup_submission(tmp_path, monkeypatch)
    session.get.side_effect = [response(HTML), response('')]
    session.post.return_value = response(body={"status": False, "msg": "token expired"})
    with pytest.raises(RetryNeeded):
        execute_work(client, *args)
    session.get.side_effect = [response(HTML), response(''), response(RECORD)]
    session.post.return_value = response()
    assert execute_work(client, *args).is_success()
    assert session.post.call_count == 2


def test_new_task_directory_reuses_course_submission_journal(tmp_path, monkeypatch):
    session, client, course, job, info, reporter = setup_submission(tmp_path, monkeypatch)
    shared = str(tmp_path / 'course-state' / 'course-1.json')
    reporter.payload['quiz_journal'] = shared
    session.get.side_effect = [response(HTML), response('')]
    session.post.side_effect = requests.Timeout()
    with pytest.raises(RetryNeeded):
        execute_work(client, course, job, info, reporter)
    next_directory = tmp_path / 'another-task'
    next_directory.mkdir()
    monkeypatch.chdir(next_directory)
    next_reporter = worker.Reporter({'quiz_journal': shared})
    session.get.side_effect = None
    session.get.return_value = response(RECORD)
    assert execute_work(client, course, job, info, next_reporter).is_success()
    assert session.post.call_count == 1
    assert client.tiku.resolve.call_count == 1


WEB_HTML = (Path(__file__).parent / 'fixtures/work-web-richtext.html').read_text()


def test_live_web_form_preserves_richtext_fields_signed_route_and_metadata():
    from urllib.parse import urlparse, parse_qs
    form = codec.decode_work_page(WEB_HTML)
    assert form['questions'][1]['blank_count'] == 2
    assert form['questions'][1]['blank_fields'] == ['answerEditor1021', 'answerEditor1022']
    data = codec.encode_form(form, ['B', ['先', '后'], False, '说明'])
    assert data['answerEditor1021'] == '先' and data['answerEditor1022'] == '后'
    assert data['answerId'] == 'metadata-answer-test'
    assert '__submit_url' not in data and 'answer102' not in data
    target = urlparse(form['__submit_url'])
    assert target.path == '/mooc-ans/work/addStudentWorkNewWeb'
    query = parse_qs(target.query)
    assert query['token'] == ['test-signature']
    assert query['version'] == ['3'] and query['ua'] == ['pc']
    assert codec.work_identifiers(WEB_HTML, 'job-fallback') == {'workId':'relation-test', 'workAnswerId':'attempt-test'}
    with pytest.raises(ValueError, match='填空声明'):
        codec.decode_work_page(WEB_HTML.replace('name="tiankongsize102" value="2"', 'name="tiankongsize102" value="3"'))
    with pytest.raises(ValueError, match='交卷地址'):
        codec.decode_work_page(WEB_HTML.replace('action="addStudentWorkNewWeb?', 'action="https://example.invalid/addStudentWorkNewWeb?'))


def test_live_web_submission_uses_relation_id_and_editor_names(tmp_path, monkeypatch):
    session, client, course, job, info, reporter = setup_submission(tmp_path, monkeypatch)
    client.tiku.resolve.return_value = ['B', ['甲','乙'], False, '说明']
    session.get.side_effect = [response(WEB_HTML), response(''), response(RECORD)]
    session.post.return_value = response()
    assert execute_work(client, course, job, info, reporter).is_success()
    assert '/addStudentWorkNewWeb?' in session.post.call_args.args[0]
    assert session.post.call_args.kwargs['data']['answerEditor1022'] == '乙'
    assert all(call.kwargs['params']['workId'] == 'relation-test' for call in session.get.call_args_list[1:])


def test_bank_query_cleans_actual_page_number_and_type_prefix(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    source = Mock()
    source._query.return_value = 'B'
    monkeypatch.setattr(quiz, 'make_source', lambda *_: source)
    question = codec.decode_work_page(WEB_HTML)['questions'][0]
    assert quiz.build_tiku({}, worker.Reporter({}), worker.NeedsAttention).resolve([question]) == ['B']
    assert source._query.call_args.args[0]['title'] == '选择语言'


def test_go_multiple_separator_matches_only_complete_options():
    q = {'type':'multiple', 'options':'A. 第一项\nB. C#\nC. C++'}
    assert codec.canonical_answer(q, '第一项#C++') == ['A','C']
    assert codec.canonical_answer(q, 'C#\nC++') == ['B','C']
    with pytest.raises(ValueError):
        codec.canonical_answer(q, '其他内容#C++')
    assert codec.canonical_answer({'type':'completion','blank_count':2}, '甲#乙') == ['甲','乙']
    assert codec.canonical_answer({'type':'completion','blank_count':2}, '甲；乙') == ['甲','乙']
    with pytest.raises(ValueError, match='分隔格式'):
        codec.canonical_answer({'type':'completion','blank_count':2}, '甲#乙；丙')


def test_reserved_zero_score_row_does_not_prove_a_formal_submission():
    session = Mock()
    session.get.return_value = response('<div>第1次作答 (提交时间： ) 本次成绩 0.0 分</div>')
    args = (session, {'courseId':'1','clazzId':'2'}, {}, {'workId':'w','workAnswerId':'a'}, 1)
    assert not real_record_is_submitted(*args)
    assert session.get.call_args.kwargs['params']['times'] == '0'
    session.get.return_value = response('<div>第1次作答 (提交时间：2026-11-14 13:50:00) 本次成绩 0.0 分</div>')
    assert real_record_is_submitted(*args)
    session.get.return_value = response('<div>提交时间： 本次成绩 0 分 题目：2026年11月14日</div>')
    assert not real_record_is_submitted(*args)


def test_expired_work_is_rejected_before_bank_queries_or_submission(tmp_path, monkeypatch):
    session, client, *args = setup_submission(tmp_path, monkeypatch)
    session.get.return_value = response(WEB_HTML + '<span>截止时间: 2026-07-01</span><span>（已截止，不能作答）</span>')
    with pytest.raises(WorkUnavailable, match='已截止'):
        execute_work(client, *args)
    client.tiku.resolve.assert_not_called()
    session.post.assert_not_called()
    from app.quiz_work import work_block_reason
    assert work_block_reason('<div style="display:none"><span style="color:red">此作业已过期</span></div>') is None


def test_configured_ai_is_automatic_fallback_without_manual_selection(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(quiz.time, 'sleep', lambda _: None)
    bank = Mock()
    bank._query.side_effect = lambda q: 'A' if q['id']=='one' else None
    monkeypatch.setattr(quiz, 'make_source', lambda *_: bank)
    ai = Mock(return_value=False)
    monkeypatch.setattr(quiz, 'query_ai', ai)
    cfg = {'provider':'TikuGo','fallbacks':[], 'endpoint':'https://example.invalid/v1','model':'test','key':'secret'}
    questions = [{'id':'one','type':'single','title':'选项','options':'A. one\nB. two'}, {'id':'two','type':'judgement','title':'判断'}]
    assert quiz.build_tiku(cfg, worker.Reporter({}), worker.NeedsAttention).resolve(questions) == ['A',False]
    assert ai.call_count == 1 and ai.call_args.args[1]['id'] == 'two'
    assert quiz.source_available('AI', {'endpoint':'http://127.0.0.1:11434/v1','model':'local','key':''})
    assert not quiz.source_available('AI', {'endpoint':'https://example.invalid/v1','model':'remote','key':''})


def test_default_single_attempt_switches_sources_for_each_question_in_order(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    events = []
    def bank(name):
        source = Mock()
        source._query.side_effect = lambda q: events.append((q['id'], name))
        return source
    sources = {name: bank(name) for name in ('TikuGo', 'TikuYanxi')}
    monkeypatch.setattr(quiz, 'make_source', lambda name, _: sources[name])
    def ai(name, question, config, repair=False):
        events.append((question['id'], name))
        assert repair is False
        return question['id'] == 'one'
    monkeypatch.setattr(quiz, 'query_ai', ai)
    pause = Mock()
    monkeypatch.setattr(quiz.time, 'sleep', pause)
    config = {'provider': 'TikuGo', 'fallbacks': ['TikuGo', 'TikuYanxi', 'TikuYanxi'],
              'tokens': 'test', 'key': 'test', 'endpoint': 'https://example.invalid/v1', 'model': 'test'}
    questions = [{'id': key, 'type': 'judgement', 'title': key} for key in ('one', 'two')]
    assert quiz.build_tiku(config, worker.Reporter({}), worker.NeedsAttention).resolve(questions) == [True, False]
    assert events == [(key, name) for key in ('one', 'two') for name in ('TikuGo', 'TikuYanxi', 'AI')]
    pause.assert_not_called()


@pytest.mark.parametrize('count', [1, 2, 3])
def test_configured_attempt_count_is_the_exact_bank_query_limit(tmp_path, monkeypatch, count):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(quiz.time, 'sleep', lambda _: None)
    bank = Mock()
    bank._query.side_effect = requests.Timeout('lookup failed')
    monkeypatch.setattr(quiz, 'make_source', lambda *_: bank)
    ai = Mock(return_value=True)
    monkeypatch.setattr(quiz, 'query_ai', ai)
    config = {'attempts': count, 'key': 'test', 'endpoint': 'https://example.invalid/v1', 'model': 'test'}
    provider = quiz.build_tiku(config, worker.Reporter({}), worker.NeedsAttention)
    assert provider.resolve([{'id': 'one', 'type': 'judgement', 'title': 'test'}]) == [True]
    assert bank._query.call_count == count and ai.call_count == 1


def test_go_single_lookup_has_no_inner_http_retry(monkeypatch):
    post = Mock(side_effect=requests.Timeout('network unavailable'))
    monkeypatch.setattr(requests, 'post', post)
    bank = quiz.make_source('TikuGo', quiz.DEFAULT_CONFIG)
    assert bank._query({'title': 'test', 'type': 'judgement'}) is None
    assert post.call_count == 1


def test_like_performs_one_lookup_instead_of_zero_or_internal_retries(monkeypatch):
    from api.answer import TikuLike
    monkeypatch.setattr(TikuLike, 'get_api_balance', lambda *_: 10)
    post = Mock(return_value=Mock(status_code=429))
    monkeypatch.setattr(requests, 'post', post)
    bank = quiz.make_source('TikuLike', {**quiz.DEFAULT_CONFIG, 'like_tokens': 'test'})
    assert bank._query({'title': 'test', 'type': 'judgement', 'options': ''}) is None
    assert post.call_count == 1


def test_yanxi_rotates_exhausted_token_without_requerying_same_attempt(monkeypatch):
    expired = Mock()
    expired.json.return_value = {'code': 0, 'data': {'times': 0, 'answer': '次数不足'}}
    success = Mock()
    success.json.return_value = {'code': 1, 'data': {'times': 9, 'answer': '正确'}}
    get = Mock(side_effect=[expired, success])
    monkeypatch.setattr(requests, 'get', get)
    bank = quiz.make_source('TikuYanxi', {**quiz.DEFAULT_CONFIG, 'tokens': 'first,second'})
    assert bank._query({'title': 'first question'}) is None
    assert get.call_count == 1
    assert get.call_args.kwargs['params']['token'] == 'first'
    assert bank._query({'title': 'second question'}) == '正确'
    assert get.call_count == 2
    assert get.call_args.kwargs['params']['token'] == 'second'


def test_record_list_timestamp_confirms_submission_without_empty_detail_timestamp(monkeypatch):
    from app.quiz_work import records
    session = Mock()
    session.get.return_value = response('''<ul class="viewMenuList">
<li data="2026-11-15 20:29:23"><span class="viewNum">第1次</span><span class="viewScore">100.0分</span></li>
</ul>''')
    found = records(session, {'courseId': '1', 'clazzId': '2', 'cpi': '3'}, {}, {'workId': 'work'})
    assert found == [(1, 100.0)]
    assert session.get.call_count == 1  # Browser copies this date into the empty detail span.


def test_reserved_or_invalid_record_timestamp_is_not_a_submission(monkeypatch):
    from app.quiz_work import records
    verify = Mock(return_value=False)
    monkeypatch.setattr('app.quiz_work.record_is_submitted', verify)
    session = Mock()
    session.get.return_value = response('''<ul class="viewMenuList">
<li data=""><span class="viewNum">第1次</span><span class="viewScore">0分</span></li>
<li data="2026-99-99 20:00:00"><span class="viewNum">第2次</span><span class="viewScore">0分</span></li>
<li data="2026-11-15 20:29:23"><span class="viewNum">第3次</span><span class="viewScore">80分</span></li>
</ul>''')
    assert records(session, {'courseId': '1', 'clazzId': '2', 'cpi': '3'}, {}, {'workId': 'work'}) == [(3, 80.0)]
    assert [call.args[-1] for call in verify.call_args_list] == [1, 2]


def test_success_navigation_is_visited_after_record_confirmation_without_reposting(tmp_path, monkeypatch):
    session, client, course, job, info, reporter = setup_submission(tmp_path, monkeypatch)
    target = 'https://mooc1.chaoxing.com/mooc-ans/api/work?courseid=1&clazzId=2&jobid=work-w1&submit=true'
    session.get.side_effect = [response(HTML), response(''), response('result'), response(RECORD)]
    session.post.return_value = response(body={'status': True, 'url': target, 'msg': 'success!'})
    assert execute_work(client, course, job, info, reporter).is_success()
    assert session.post.call_count == 1
    navigation = [call for call in session.get.call_args_list if call.args[0] == target]
    assert len(navigation) == 1 and navigation[0].kwargs['allow_redirects'] is False
    entry = json.loads((tmp_path / 'quiz-submissions.json').read_text())['work-w1']
    assert entry['phase'] == 'confirmed' and entry['completion_visited']
    session.get.side_effect = None
    session.get.return_value = response(RECORD)
    assert execute_work(client, course, job, info, reporter).is_success()
    assert session.post.call_count == 1  # A later retry does not resubmit or revisit.


def test_completion_navigation_failure_retries_only_get_not_submit(tmp_path, monkeypatch):
    session, client, course, job, info, reporter = setup_submission(tmp_path, monkeypatch)
    target = '/mooc-ans/api/work?courseid=1&clazzId=2&jobid=work-w1&submit=true'
    session.get.side_effect = [response(HTML), response(''), requests.Timeout()]
    session.post.return_value = response(body={'status': True, 'url': target})
    with pytest.raises(RetryNeeded, match='只重试确认'):
        execute_work(client, course, job, info, reporter)
    session.get.side_effect = [response('result'), response(RECORD)]
    assert execute_work(client, course, job, info, reporter).is_success()
    assert session.post.call_count == 1


def test_foreign_success_url_never_receives_authenticated_request(tmp_path, monkeypatch):
    session, client, course, job, info, reporter = setup_submission(tmp_path, monkeypatch)
    session.get.side_effect = [response(HTML), response('')]
    session.post.return_value = response(body={'status': True, 'url': 'https://example.invalid/collect'})
    with pytest.raises(WorkUnavailable, match='确认链接'):
        execute_work(client, course, job, info, reporter)
    assert session.get.call_count == 2 and session.post.call_count == 1


def test_json_error_preserves_exact_exception_and_raw_private_response(tmp_path, monkeypatch):
    session, client, course, job, info, reporter = setup_submission(tmp_path, monkeypatch)
    session.get.side_effect = [response(HTML), response('')]
    result = response('<html>gateway failure</html>')
    result.headers = {'Content-Type': 'text/html'}
    result.json.side_effect = ValueError('invalid response json')
    session.post.return_value = result
    with pytest.raises(RetryNeeded, match='ValueError'):
        execute_work(client, course, job, info, reporter)
    evidence = json.loads((tmp_path / 'submit-response.json').read_text())
    assert evidence['exception_type'] == 'ValueError' and evidence['http_status'] == 200
    assert evidence['response_body'] == '<html>gateway failure</html>'
    assert (tmp_path / 'submit-response.json').stat().st_mode & 0o777 == 0o600
    entry = json.loads((tmp_path / 'quiz-submissions.json').read_text())['work-w1']
    assert entry['phase'] == 'intent' and 'response_body' not in entry['submit_diagnostic']
