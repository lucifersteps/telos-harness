"""Native history search/read across real compaction, restart and channel boundaries."""
import asyncio
import json
import re
import pytest
from support import Harness, call
from test_desktop import Desktop, DESKTOP
from test_package import ARCHIVE, run_async
from settings import read_config, write_config

MARKER='ORIGINALHISTORY'
FACT='红杉面馆，暗号是蓝色风铃。'

def results(request):
    return [b for m in request['messages'] if isinstance(m.get('content'),list)
            for b in m['content'] if b.get('type')=='tool_result']

def tool_text(result):
    return '\n'.join(b.get('text','') for b in result['content'])

def lookup_script(done,query=MARKER):
    def read_hit(request):
        text=tool_text(results(request)[-1])
        match=re.search(r'seq (\d+)',text)
        return call('history-read',{'seq':int(match[1]) if match else 999999,'before':1,'after':1},'session_event_read')
    return [call('history-search',{'query':query,'event_types':['user/message'],'surfaces':['shadowed']},'session_event_search'),read_hit,done]

def assert_recalled(model):
    search,read=results(model.requests[-1])[-2:]
    assert not search.get('is_error') and not read.get('is_error')
    assert 'shadowed' in tool_text(search)
    text=tool_text(read)
    assert MARKER in text and FACT in text
    assert 'Before:' in text and 'After:' in text
    names=[t.get('name') or t['function']['name'] for t in model.requests[-1]['tools']]
    assert names.count('session_event_search')==names.count('session_event_read')==1
    assert not {'session_search','session_trace','session_event_trace'} & set(names)

def pressure_script(model):
    def large(request):
        model.input_tokens=800000
        return 'A'*600000
    def summary(request):
        model.input_tokens=10
        return 'The user supplied a large temporary record. Earlier restaurant details were omitted.'
    return [large,summary]


def test_cli_history_compaction_restart_and_other_session_denial(tmp_path):
    async def run():
        h=Harness(tmp_path,['其他会话已记录。'])
        try:
            # A real neighboring session with the same user directory is not authorized.
            h.session='other-wechat-session'
            await h.submit('OTHERSESSIONPRIVATE 不应从其他会话读取。')
            h.session='wechat-test';h.model.script=['已记下。']
            await h.submit(MARKER+' '+FACT)
            h.model.script=pressure_script(h.model)+lookup_script('压缩后找回。')
            await h.submit('B'*600000)
            await h.submit('请回查之前提到的餐厅')
            assert h.sent[-1]=='压缩后找回。'
            assert_recalled(h.model)
            assert any(e['type']=='compaction/summary' for e in h.events)
            await h.runtime.close();h.runtime=h.restart()
            h.model.script=lookup_script('重启后找回。',query='红杉面馆')
            await h.submit('再查一遍历史')
            assert_recalled(h.model)
            for name,args in [('session_event_search',{'query':'OTHERSESSIONPRIVATE'}),('session_event_read',{'seq':0})]:
                h.model.script=[call('forbidden',{**args,'session_id':'other-wechat-session'},name),'拒绝跨会话。']
                await h.submit('尝试指定其他会话')
                result=results(h.model.requests[-1])[-1]
                assert result['is_error'] and 'TELOS_HISTORY_CURRENT_SESSION_ONLY' in tool_text(result)
            h.model.script=[call('own-only',{'query':'OTHERSESSIONPRIVATE','event_types':['user/message']},'session_event_search'),'当前会话没有此记录。']
            await h.submit('仅搜索当前历史')
            assert 'OTHERSESSIONPRIVATE' not in tool_text(results(h.model.requests[-1])[-1])
        finally:await h.close()
    asyncio.run(run())


@pytest.mark.skipif(DESKTOP is None or not ARCHIVE.exists(),reason='Requires official desktop and built archive')
@run_async
async def test_desktop_history_compaction_and_resume(tmp_path):
    d=Desktop(tmp_path);d.model.script=['已记下。']
    async def send(key,text,done):
        count=len(d.services.sent);d.services.push(key,text)
        await d.wait(lambda:any(done in str(x) for x in d.services.sent[count:]),timeout=45)
    try:
        await d.start();await d.action('save',tools={'apple':False,'maps':False})
        root=d.home/'telos';config=read_config(root)
        config['wechat'].update(base_url=d.services.url,cdn_base_url=d.services.url);write_config(root,config)
        await d.action('login');await d.action('start')
        await send('remember',MARKER+' '+FACT,'已记下。')
        d.model.script=pressure_script(d.model)+lookup_script('压缩后找回。')
        await send('pressure','B'*600000,'A'*100)
        await send('recall','请回查之前提到的餐厅','压缩后找回。')
        assert_recalled(d.model)
        assert d.manage('independent')['historySearchMode']=='never'
        d.stop();await d.start();await d.wait(lambda:d.get()['running'])
        d.model.script=lookup_script('重启后找回。',query='红杉面馆')
        await send('resume','再查一遍历史','重启后找回。')
        assert_recalled(d.model)
        for i,(name,args) in enumerate([('session_event_search',{'query':'private'}),('session_event_read',{'seq':0})]):
            d.model.script=[call('forbidden',{**args,'session_id':'another-session'},name),'拒绝跨会话。']
            await send('forbidden-'+str(i),'尝试指定其他会话','拒绝跨会话。')
            result=results(d.model.requests[-1])[-1]
            assert result['is_error'] and 'TELOS_HISTORY_CURRENT_SESSION_ONLY' in tool_text(result)
    finally:await d.close()
