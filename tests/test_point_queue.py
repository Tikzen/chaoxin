import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import pytest

from app import store
from app.point_queue import PointLedger, point_key


def launch(tmp_path, concurrency=3, total=3, limit=None):
    payload={'action':'learn','mode':'demo','credentials':{},'course':{'total':total,'done':0},
             'point_concurrency':concurrency,'point_store':str(tmp_path/'points.sqlite'),
             'runtime_path':str(tmp_path/'runtime.json'),'task_point_limit':limit}
    store.write_json(tmp_path/'runtime.json',{'point_concurrency':concurrency})
    env={**os.environ,'DEMO_TICK_SECONDS':'0.08'}
    process=subprocess.Popen([sys.executable,str(store.ROOT/'app/worker.py')],cwd=tmp_path,stdin=subprocess.PIPE,
                             stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,env=env,start_new_session=True)
    process.stdin.write(json.dumps(payload).encode());process.stdin.close()
    return process,payload


def state(tmp_path):return store.read_json(tmp_path/'state.json',{})


def wait_until(predicate,timeout=25):
    end=time.monotonic()+timeout
    while time.monotonic()<end:
        result=predicate()
        if result:return result
        time.sleep(.04)
    pytest.fail('Condition did not become true')


def cleanup(process):
    try:os.killpg(process.pid,signal.SIGKILL)
    except ProcessLookupError:pass
    process.wait(timeout=5)


@pytest.mark.parametrize('count',[1,2,3])
def test_real_process_fifo_has_exact_slot_count_and_no_duplicates(tmp_path,count):
    process,_=launch(tmp_path,count)
    try:
        wait_until(lambda:process.poll() is not None)
        value=state(tmp_path)
        assert value['status']=='completed',value
        assert value['point_peak']==count
        history=value['point_history']
        assert len(history)==len({p['id'] for p in history})==3
        assert len({p['pid'] for p in history})==3
        assert all(p['status']=='completed' for p in history)
        events=[]
        for file in (tmp_path/'units').glob('*/state.json'):
            item=json.loads(file.read_text())
            events.extend([(datetime.fromisoformat(item['started_at']),1),(datetime.fromisoformat(item['updated_at']),-1)])
        active=peak=0
        for _,delta in sorted(events):active+=delta;peak=max(peak,active)
        assert peak==count  # Actual child execution intervals, not Popen counts.
    finally:cleanup(process)


@pytest.mark.parametrize('high,total', [(3,9),(10,16)])
def test_live_resize_does_not_restart_claimed_points(tmp_path,high,total):
    process,_=launch(tmp_path,1,total=total)
    try:
        wait_until(lambda:state(tmp_path).get('active_point_count')==1)
        first=state(tmp_path)['active_points'][0]['pid']
        store.write_json(tmp_path/'runtime.json',{'point_concurrency':high})
        wait_until(lambda:state(tmp_path).get('active_point_count')==high)
        initial={p['pid'] for p in state(tmp_path)['active_points']}
        assert first in initial
        for count in (2,1):
            store.write_json(tmp_path/'runtime.json',{'point_concurrency':count})
            wait_until(lambda:state(tmp_path).get('point_concurrency')==count)
        # All current owners finish; a newly dispatched worker must run alone.
        def serial_dispatch():
            value=state(tmp_path);rows=value.get('active_points',[])
            fresh=[p for p in rows if p['pid'] not in initial]
            if fresh:assert len(rows)==1
            return bool(fresh)
        wait_until(serial_dispatch)
        store.write_json(tmp_path/'runtime.json',{'point_concurrency':2})
        wait_until(lambda:state(tmp_path).get('point_concurrency')==2)
        store.write_json(tmp_path/'runtime.json',{'point_concurrency':3})
        wait_until(lambda:process.poll() is not None,timeout=30)
        value=state(tmp_path)
        assert value['status']=='completed',value
        assert len(value['point_history'])==total
        assert len({p['id'] for p in value['point_history']})==total
        assert len({p['pid'] for p in value['point_history']})==total
    finally:cleanup(process)


def test_exclusive_claim_blocks_other_process_and_completed_is_never_reclaimed(tmp_path):
    ledger=PointLedger(tmp_path/'points.sqlite')
    point={'id':'1'};job={'jobid':'job-1','type':'video'};key=point_key(point,job)
    unit={'id':key,'point':point,'job':job,'name':'video'}
    fd=ledger.claim(unit,'first')
    assert fd is not None
    script='''import json,sys,os
from app.point_queue import PointLedger
ledger=PointLedger(sys.argv[1]);fd=ledger.claim(json.loads(sys.argv[2]),'second')
print('blocked' if fd is None else 'claimed')
if fd is not None:os.close(fd)
'''
    try:
        result=subprocess.run([sys.executable,'-c',script,str(ledger.path),json.dumps(unit)],cwd=store.ROOT,capture_output=True,text=True,timeout=5)
        assert result.stdout.strip()=='blocked'
    finally:os.close(fd)
    replacement=ledger.claim(unit,'after-crash')
    assert replacement is not None
    ledger.update(key,'first','processed')  # A stale owner cannot overwrite it.
    assert ledger.get(key)['owner']=='after-crash' and ledger.get(key)['status']=='running'
    ledger.complete(key);os.close(replacement)
    assert ledger.claim(unit,'third') is None


def test_limited_scope_stays_fixed_across_retries(tmp_path):
    process,payload=launch(tmp_path,3,total=6,limit=3)
    try:
        wait_until(lambda:process.poll() is not None)
        value=state(tmp_path)
        assert value['partial_scope'] and value['done']==3 and value['progress']==50
        selected=json.loads((tmp_path/'point-scope.json').read_text())
        assert len(selected)==3
        payload['course']['done']=3
        env={**os.environ,'DEMO_TICK_SECONDS':'0.01'}
        second=subprocess.run([sys.executable,str(store.ROOT/'app/worker.py')],cwd=tmp_path,input=json.dumps(payload),
                              capture_output=True,text=True,env=env,timeout=10)
        assert second.returncode==0
        assert len(list((tmp_path/'units').iterdir()))==3
        assert state(tmp_path)['points_processed']==0
    finally:cleanup(process)


def test_parallel_journals_merge_without_losing_other_quiz_intents(tmp_path):
    script='''import sys,time,os
from pathlib import Path
from types import SimpleNamespace
from app.quiz_work import Journal
root=Path(sys.argv[1]);key=sys.argv[2]
j=Journal(SimpleNamespace(payload={'quiz_journal':str(root/'journal.json')}))
(root/('ready-'+key)).touch()
while not (root/'go').exists():time.sleep(.01)
j.save(key,{'phase':'intent','form':{'workId':key}})
j.save(key,{'phase':'confirmed','form':{'workId':key}})
'''
    processes=[subprocess.Popen([sys.executable,'-c',script,str(tmp_path),str(i)],cwd=store.ROOT) for i in range(3)]
    try:
        wait_until(lambda:len(list(tmp_path.glob('ready-*')))==3)
        (tmp_path/'go').touch()
        for process in processes:assert process.wait(timeout=10)==0
        data=json.loads((tmp_path/'journal.json').read_text())
        assert set(data)=={'0','1','2'}
        assert all(value['phase']=='confirmed' for value in data.values())
    finally:
        for process in processes:
            if process.poll() is None:process.kill();process.wait()


def test_discovery_deduplicates_and_preserves_authoritative_completion(tmp_path,monkeypatch):
    from unittest.mock import Mock
    monkeypatch.syspath_prepend(str(store.ROOT/'vendor/chaoxing'))
    from api.base import SessionManager
    from app.parallel import read_units
    card={'type':'video','job':True,'jobid':'v1','mid':'m','objectId':'obj','isPassed':False,'property':{'name':'clip'}}
    passed={**card,'jobid':'v-done','isPassed':True}
    data={'defaults':{'knowledgeid':'p','cpi':'c','ktoken':'k'},'attachments':[card,card,passed]}
    response=Mock();response.text='mArg='+json.dumps(data)+';'
    session=Mock();session.get.return_value=response
    monkeypatch.setattr(SessionManager,'get_session',lambda:session)
    ledger=PointLedger(tmp_path/'points.sqlite')
    points=[{'id':'locked','title':'locked','need_unlock':True},{'id':'p','title':'chapter'}]
    units=list(read_units(Mock(),{'courseId':'1','clazzId':'2','cpi':'c'},points,Mock(),ledger))
    assert len(units)==1 and units[0]['job']['jobid']=='v1'
    assert ledger.get(point_key(points[1],passed))['status']=='completed'
    assert session.get.call_count==7  # Repeated cards do not hide later distinct tasks.


def test_job_flag_disappearance_confirms_prior_owner_without_replaying_video(tmp_path,monkeypatch):
    from unittest.mock import Mock
    monkeypatch.syspath_prepend(str(store.ROOT/'vendor/chaoxing'))
    from api.base import SessionManager
    from app.parallel import read_units
    ledger=PointLedger(tmp_path/'points.sqlite');point={'id':'p','title':'chapter'}
    job={'type':'video','jobid':'v1'};key=point_key(point,job)
    unit={'id':key,'point':point,'job':job,'name':'clip'}
    fd=ledger.claim(unit,'prior');ledger.update(key,'prior','processed');os.close(fd)
    finished={'type':'video','jobid':'v1','mid':'m','objectId':'one','property':{'name':'done'}}
    pending={**finished,'jobid':'v2','job':True,'objectId':'two'}
    response=Mock();response.text='mArg='+json.dumps({'defaults':{'knowledgeid':'p'},'attachments':[finished,pending]})+';'
    session=Mock();session.get.return_value=response
    monkeypatch.setattr(SessionManager,'get_session',lambda:session)
    result=list(read_units(Mock(),{'courseId':'1','clazzId':'2','cpi':'c'},[point],Mock(),ledger))
    assert [u['job']['jobid'] for u in result]==['v2']
    assert ledger.get(key)['status']=='completed'
    assert ledger.claim(unit,'new') is None


@pytest.mark.parametrize('transient_failure', [False, True])
def test_delayed_platform_completion_is_polled_while_long_worker_keeps_running(tmp_path, monkeypatch, transient_failure):
    from unittest.mock import Mock
    from app import parallel, worker
    monkeypatch.chdir(tmp_path)
    clock = [0.0]
    monkeypatch.setattr(parallel.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(parallel.time, 'sleep', lambda seconds: clock.__setitem__(0, clock[0] + 31))
    short = {'id': 'short', 'title': 'short', 'has_finished': False}
    long = {'id': 'long', 'title': 'long', 'has_finished': False}
    unit = {'id': 'long-point', 'point': long, 'job': {'type': 'video', 'jobid': 'v'}, 'name': 'long video'}
    reads = [0]
    def chapters(*args):
        reads[0] += 1
        if transient_failure and reads[0] == 3:
            raise OSError('temporary read failure')
        return {'points': [{**short, 'has_finished': reads[0] >= 3},
                           {**long, 'has_finished': reads[0] >= 4 + int(transient_failure)}]}
    client = Mock()
    course = {'courseId': '1', 'clazzId': '2', 'cpi': '3'}
    client.get_course_list.return_value = [course]
    client.get_course_point.side_effect = chapters
    monkeypatch.setattr(worker, 'platform_client', lambda *args: client)
    # The short point has just finished locally, but its first platform read
    # still says pending. Only the existing long worker needs to stay alive.
    monkeypatch.setattr(parallel, 'read_units', lambda client, course, points, reporter, ledger:
        iter([unit] if any(p['id'] == 'long' and not p['has_finished'] for p in points) else []))
    observations, starts = [], []
    class Pool:
        def __init__(self, payload, reporter, ledger, client):
            self.active = {}
            self.finished = {'short-point': {'point': short}}
            self.errors = []
            self.ticks = 0
        def collect(self):
            self.ticks += 1
            if self.ticks >= 4 + int(transient_failure) and self.active:
                self.active.clear()
                self.finished[unit['id']] = unit
        def slots(self): return 3
        def start(self, value):
            starts.append(value['id'])
            self.active[value['id']] = {'pid': 1234}
            return True
        def publish(self, done, total):
            observations.append((done, tuple(self.active)))
        def close(self):
            assert not self.active, 'Refresh interrupted the long worker'
    monkeypatch.setattr(parallel, 'PointPool', Pool)
    reporter = Mock()
    parallel.learn_parallel({'mode': 'real', 'course': course, 'point_concurrency': 3,
                            'point_store': str(tmp_path / 'points.sqlite')}, reporter)
    assert starts == ['long-point']
    assert (1, ('long-point',)) in observations  # Updated without another child exit.
    assert observations[-1] == (2, ())
    assert reporter.update.call_args.kwargs['active_point_count'] == 0
    if transient_failure:
        assert any('继续执行' in call.args[0] for call in reporter.log.call_args_list)


def test_explicit_empty_cards_enqueue_one_read_ack_without_implicit_platform_writes(tmp_path, monkeypatch):
    from unittest.mock import Mock
    from app.parallel import read_units
    from test_progress import EMPTY_CARD
    monkeypatch.syspath_prepend(str(store.ROOT / 'vendor/chaoxing'))
    from api.base import SessionManager
    session, client = Mock(), Mock()
    session.get.return_value.text = EMPTY_CARD
    monkeypatch.setattr(SessionManager, 'get_session', lambda: session)
    ledger = PointLedger(tmp_path / 'points.sqlite')
    point = {'id': 'p', 'title': '空章节', 'jobCount': 1}
    course = {'courseId': '1', 'clazzId': '2', 'cpi': '3'}
    units = list(read_units(client, course, [point], Mock(), ledger))
    assert len(units) == 1 and units[0]['job']['type'] == 'empty'
    assert session.get.call_count == 7
    assert ledger.get(units[0]['id']) is None  # Discovery does not fake completion.
    client.study_emptypage.assert_not_called()
    ledger.complete(units[0]['id'], point, units[0]['job'])
    assert list(read_units(client, course, [point], Mock(), ledger)) == []


def test_empty_first_card_does_not_hide_a_later_video(tmp_path, monkeypatch):
    from unittest.mock import Mock
    from app.parallel import read_units
    from test_progress import EMPTY_CARD
    monkeypatch.syspath_prepend(str(store.ROOT / 'vendor/chaoxing'))
    from api.base import SessionManager
    video = {'type': 'video', 'job': True, 'jobid': 'v1', 'mid': 'm', 'objectId': 'o', 'property': {'name': 'later video'}}
    html = 'mArg=' + json.dumps({'defaults': {'knowledgeid': 'p'}, 'attachments': [video]}) + ';'
    session = Mock()
    session.get.side_effect = [Mock(text=EMPTY_CARD), Mock(text=html)] + [Mock(text=EMPTY_CARD) for _ in range(5)]
    monkeypatch.setattr(SessionManager, 'get_session', lambda: session)
    units = list(read_units(Mock(), {'courseId': '1', 'clazzId': '2', 'cpi': '3'},
                            [{'id': 'p', 'title': 'later card'}], Mock(), PointLedger(tmp_path / 'points.sqlite')))
    assert [u['job']['type'] for u in units] == ['video']
    assert session.get.call_count == 7


def test_two_ordinary_text_cards_do_not_hide_later_required_work(tmp_path, monkeypatch):
    from unittest.mock import Mock
    from app.parallel import read_units
    from test_progress import EMPTY_CARD
    monkeypatch.syspath_prepend(str(store.ROOT / 'vendor/chaoxing'))
    from api.base import SessionManager
    text = 'mArg=' + json.dumps({'defaults': {'knowledgeid': 'p'}, 'attachments': []}) + ';'
    work = {'type': 'workid', 'job': True, 'jobid': 'work-later', 'property': {'name': 'later quiz'}}
    html = 'mArg=' + json.dumps({'defaults': {'knowledgeid': 'p'}, 'attachments': [work]}) + ';'
    session = Mock()
    session.get.side_effect = [Mock(text=text), Mock(text=text), Mock(text=html)] + [Mock(text=EMPTY_CARD) for _ in range(4)]
    monkeypatch.setattr(SessionManager, 'get_session', lambda: session)
    units = list(read_units(Mock(), {'courseId': '1', 'clazzId': '2', 'cpi': '3'},
                            [{'id': 'p', 'title': 'later work'}], Mock(), PointLedger(tmp_path / 'points.sqlite')))
    assert [u['job']['type'] for u in units] == ['workid']
    assert session.get.call_count == 7


@pytest.mark.parametrize('bad_page', ['<html>请重新登录</html>', '章节未开放', 'mArg={};'])
def test_unknown_or_locked_card_never_becomes_a_read_ack(tmp_path, monkeypatch, bad_page):
    from unittest.mock import Mock
    from app.parallel import read_units
    from test_progress import EMPTY_CARD
    monkeypatch.syspath_prepend(str(store.ROOT / 'vendor/chaoxing'))
    from api.base import SessionManager
    session = Mock()
    session.get.side_effect = [Mock(text=EMPTY_CARD), Mock(text=bad_page)]
    monkeypatch.setattr(SessionManager, 'get_session', lambda: session)
    assert list(read_units(Mock(), {'courseId': '1', 'clazzId': '2', 'cpi': '3'},
                           [{'id': 'p', 'title': 'uncertain'}], Mock(), PointLedger(tmp_path / 'points.sqlite'))) == []


@pytest.mark.parametrize('confirmed', [False, True])
def test_only_unconfirmed_point_errors_can_force_course_retry(tmp_path, monkeypatch, confirmed):
    from unittest.mock import Mock
    from app import parallel, worker
    from app.retry import RetryNeeded
    monkeypatch.chdir(tmp_path)
    course = {'courseId': '1', 'clazzId': '2', 'cpi': '3'}
    point = {'id': 'p', 'title': 'quiz', 'has_finished': confirmed}
    job = {'type': 'workid', 'jobid': 'work-one'}
    key = point_key(point, job)
    unit = {'id': key, 'point': point, 'job': job, 'name': 'quiz', 'info': {}}
    path = tmp_path / 'points.sqlite'
    ledger = PointLedger(path)
    fd = ledger.claim(unit, 'old')
    ledger.update(key, 'old', 'retry', error='record was pending')
    os.close(fd)
    original_pool = parallel.PointPool
    class Pool(original_pool):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.errors = [(key, 'retry_wait', 'record was pending')]
            self.finished[key] = {**unit, 'status': 'retry_wait', 'error': 'record was pending', 'pid': 1234}
    monkeypatch.setattr(parallel, 'PointPool', Pool)
    monkeypatch.setattr(parallel, 'read_units', lambda *args: iter([]))
    client = Mock()
    client.get_course_list.return_value = [course]
    client.get_course_point.return_value = {'points': [point]}
    monkeypatch.setattr(worker, 'platform_client', lambda *args: client)
    reporter = worker.Reporter({})
    payload = {'mode': 'real', 'course': course, 'point_store': str(path), 'point_concurrency': 1}
    if confirmed:
        parallel.learn_parallel(payload, reporter)
        assert reporter.state['status'] == 'completed'
        assert reporter.state['point_history'][0]['error'] == ''
    else:
        with pytest.raises(RetryNeeded, match='record was pending'):
            parallel.learn_parallel(payload, reporter)
        assert reporter.state['point_history'][0]['error'] == 'record was pending'


@pytest.mark.parametrize('scope_limit', [None, 1])
def test_newly_unlocked_chapter_continues_same_run_without_consuming_retry_budget(tmp_path, monkeypatch, scope_limit):
    from unittest.mock import Mock
    from app import parallel, worker
    monkeypatch.chdir(tmp_path)
    course = {'courseId': '1', 'clazzId': '2', 'cpi': '3'}
    started = []
    def points(*args):
        return {'points': [{'id': 'a', 'title': 'first', 'has_finished': 'a' in started},
                           {'id': 'b', 'title': 'next', 'has_finished': 'b' in started, 'need_unlock': 'a' not in started}]}
    def discover(client, course, chapters, reporter, ledger):
        for p in chapters:
            if not p.get('has_finished') and not p.get('need_unlock'):
                job = {'type': 'video', 'jobid': p['id']}
                yield {'id': point_key(p, job), 'point': p, 'job': job, 'info': {}, 'name': p['title']}
    original_pool = parallel.PointPool
    class Pool(original_pool):
        def start(self, unit):
            started.append(unit['point']['id'])
            self.ledger.complete(unit['id'], unit['point'], unit['job'])
            self.finished[unit['id']] = {**unit, 'status': 'completed', 'error': '', 'pid': 1234}
            return True
    monkeypatch.setattr(parallel, 'PointPool', Pool)
    monkeypatch.setattr(parallel, 'read_units', discover)
    client = Mock()
    client.get_course_list.return_value = [course]
    client.get_course_point.side_effect = points
    monkeypatch.setattr(worker, 'platform_client', lambda *args: client)
    reporter = worker.Reporter({})
    parallel.learn_parallel({'mode': 'real', 'course': course, 'point_concurrency': 3,
                             'task_point_limit': scope_limit, 'point_store': str(tmp_path / 'points.sqlite')}, reporter)
    assert reporter.state['status'] == 'completed'
    assert started == (['a', 'b'] if scope_limit is None else ['a'])
    assert not reporter.state.get('retry_count')
    if scope_limit:
        assert reporter.state['partial_scope'] and reporter.state['done'] == 1
