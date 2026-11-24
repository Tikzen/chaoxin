import json
from pathlib import Path
from unittest.mock import Mock

import pytest
import requests

from app import quiz_codec as codec, worker
from app.quiz_redo import RedoPermits
from app.quiz_work import Journal, execute_work, redo_remaining
from app.question_dom import feedback_answers
from app.retry import RetryNeeded, WorkUnavailable

BASE = (Path(__file__).parent / 'fixtures/work-five-types.html').read_text()


def test_reads_only_explicit_remaining_redo_count():
    assert redo_remaining('<button>重做（剩余 100 次）</button>') == 100
    assert redo_remaining('<button>重做（剩余0次）</button>') == 0
    assert redo_remaining('<div>重做</div>') is None


def shuffled_html():
    return BASE.replace('<li aria-label="A. C++">A. C++</li><li aria-label="B. Python">B. Python</li>', '''
        <li aria-label="C 选择"><span class="num_option" data="C">A</span><a>C++</a></li>
        <li aria-label="A 选择"><span class="num_option" data="A">B</span><a>Python</a></li>''')


def test_visible_letters_are_encoded_to_platform_shuffled_values():
    form = codec.decode_work_page(shuffled_html())
    assert [(c['label'], c['text'], c['submit_value']) for c in form['questions'][0]['choices']] == [('A', 'C++', 'C'), ('B', 'Python', 'A')]
    assert codec.encode_form(form, ['A', 'AC', ['甲', '乙'], False, '说明'])['answer1'] == 'C'
    assert codec.encode_form(form, ['B', 'AC', ['甲', '乙'], False, '说明'])['answer1'] == 'A'
    bad = shuffled_html().replace('data="A">B', 'data="C">B')
    with pytest.raises(ValueError, match='重复'):
        codec.decode_work_page(bad)


def test_formula_metadata_is_preserved_as_text_alongside_image():
    from urllib.parse import quote
    formula = quote(json.dumps(r'f(x)=\sin(x)', ensure_ascii=False))
    html = BASE.replace('哪种语言？', f'<img class="ans-latex-moudle" data="{formula}" src="https://p.ananas.chaoxing.com/f.png">的导数？')
    question = codec.decode_work_page(html)['questions'][0]
    assert r'f(x)=\sin(x)' in question['title'] and question['image_sources']


def test_platform_feedback_matches_content_instead_of_reusing_letters():
    form = codec.decode_work_page(shuffled_html())
    detail = '''<div class="singleQuesId" data="1"><ul>
      <li><i class="fl">A、</i><a>Python</a></li><li><i class="fl">B、</i><a>C++</a></li></ul>
      <div class="correctAnswer"><div class="answerCon">B</div></div></div>'''
    assert feedback_answers(detail, form['questions']) == {'1': 'A'}
    assert feedback_answers(detail.replace('C++', 'changed'), form['questions']) == {}
    assert feedback_answers(detail.replace('data="1"', 'data="999"'), form['questions']) == {}


def test_redo_allowance_is_durable_atomic_and_cannot_be_reset_by_repeating_grant(tmp_path):
    path = tmp_path / 'points.sqlite'; first = RedoPermits(path)
    first.grant_once('work-one', 1, 'user authorized one redo')
    assert first.available('work-one', 1) and not first.available('work-one', 2)
    assert RedoPermits(path).consume('work-one', 1)
    first.grant_once('work-one', 1, 'same authorization repeated')
    assert not first.available('work-one', 1) and not first.consume('work-one', 1)


def setup(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / 'vendor/chaoxing'))
    from api.base import SessionManager
    from api.logger import logger
    logger.remove()
    session = Mock(); monkeypatch.setattr(SessionManager, 'get_session', lambda: session)
    html = Mock(text=BASE, url='https://mooc1.chaoxing.com/mooc-ans/work/doHomeWorkNew')
    detail = Mock(text='<html>no published corrections</html>')
    session.get.side_effect = [html, detail]
    session.post.return_value.json.return_value = {'status': True, 'msg': '提交成功'}
    reporter = worker.Reporter({'point_store': str(tmp_path / 'points.sqlite')})
    journal = Journal(reporter); journal.save('work-w1', {'phase': 'confirmed', 'form': {'workId': 'w1', 'workAnswerId': 'a1'}, 'score': 20})
    permits = RedoPermits(reporter.payload['point_store']); permits.grant_once('work-w1', 1, 'authorized')
    client = Mock(); client.tiku.resolve.return_value = ['A', 'AC', ['甲', '乙'], False, '说明']
    args = (client, {'courseId': '1', 'clazzId': '2', 'cpi': '3'}, {'jobid': 'work-w1', 'enc': 'e'}, {'knowledgeid': 'k', 'ktoken': 't', 'cpi': '3'}, reporter)
    return session, permits, reporter, args


def test_authorized_redo_submits_once_and_confirms_only_new_attempt(tmp_path, monkeypatch):
    session, permits, reporter, args = setup(tmp_path, monkeypatch)
    records = Mock(side_effect=[[(1, 20)], [(1, 20)], [(1, 20), (2, 100)]])
    monkeypatch.setattr('app.quiz_work.records', records)
    assert execute_work(*args).is_success()
    assert session.post.call_count == 1 and not permits.available('work-w1', 1)
    assert Journal(reporter).data['work-w1']['score'] == 100
    records.side_effect = None; records.return_value = [(1, 20), (2, 100)]
    assert execute_work(*args).is_success() and session.post.call_count == 1


@pytest.mark.parametrize('failure', ['timeout', 'invalid_answers'])
def test_redo_failure_never_repeats_a_submission_or_accepts_old_record(tmp_path, monkeypatch, failure):
    session, permits, reporter, args = setup(tmp_path, monkeypatch)
    monkeypatch.setattr('app.quiz_work.records', lambda *a: [(1, 20)])
    if failure == 'timeout':
        session.post.side_effect = requests.Timeout()
    else:
        args[0].tiku.resolve.side_effect = RetryNeeded('no valid answers')
    with pytest.raises(RetryNeeded):execute_work(*args)
    if failure == 'timeout':
        session.get.side_effect = None
        with pytest.raises(RetryNeeded):execute_work(*args)
        assert session.post.call_count == 1 and not permits.available('work-w1', 1)
    else:
        session.post.assert_not_called()
        assert permits.available('work-w1', 1)


def test_no_permit_reports_actual_low_score_instead_of_generic_unconfirmed_success(tmp_path, monkeypatch):
    session, permits, reporter, args = setup(tmp_path, monkeypatch)
    permits.consume('work-w1', 1); reporter.payload['action'] = 'job'
    monkeypatch.setattr('app.quiz_work.records', lambda *a: [(1, 20)])
    with pytest.raises(WorkUnavailable, match='20.*仍未通过'):
        execute_work(*args)
    session.post.assert_not_called()
