"""Native request wire, persistence and real compaction hooks with fake I/O."""
import asyncio
import json
from support import Harness, call, draft


def states(request):
    result=[]
    for message in request['messages']:
        blocks=message.get('content', [])
        if isinstance(blocks,str):blocks=[{'type':'text','text':blocks}]
        for block in blocks:
            text=block.get('text','')
            if text.startswith('Current Telos state ('):
                result.append(json.loads(text.split('\n',1)[1]))
    return result


def test_replacement_tool_continuation_and_process_restart(tmp_path):
    async def run():
        h=Harness(tmp_path,[call('draft',draft()),'请确认。'])
        try:
            await h.submit('创建会议')
            assert len(h.model.requests)==2
            assert all(len(states(r))==1 for r in h.model.requests)
            assert states(h.model.requests[-1])[0]['current']['telos:business']['pending']
            h.model.script=[call('read',{'action':'read'},'user_memory'),'确认后的回复。']
            await h.submit('1')
            assert len(h.native.writes)==1
            latest=states(h.model.requests[-1])[0]
            assert not latest['current']['telos:business'].get('pending')
            assert latest['current']['telos:business']['receipt']
            assert all(len(states(r))==1 for r in h.model.requests)
            assert any(e.get('surfaceOp',{}).get('op')=='replace' for e in h.events if isinstance(e.get('surfaceOp'),dict)
                       and e['type']=='user/message' and e['data'].get('source',{}).get('kind')=='telos-current-state')
            await h.runtime.close();h.runtime=h.restart();h.model.script=['恢复后的回复。']
            await h.submit('继续聊')
            assert h.sent[-1]=='恢复后的回复。'
            assert len(states(h.model.requests[-1]))==1
            assert '创建会议' in json.dumps(h.model.requests[-1],ensure_ascii=False)
            assert len(h.native.writes)==1
        finally:await h.close()
    asyncio.run(run())


def test_state_returns_after_native_compaction_and_restart(tmp_path):
    async def run():
        h=Harness(tmp_path,[])
        def large(_):h.model.input_tokens=800000;return 'A'*600000
        def summary(_):h.model.input_tokens=10;return 'A long conversation was summarized. No current-state details retained.'
        h.model.script=[large,summary,'压缩后回复。','重启后回复。']
        try:
            await h.submit('B'*600000)
            await h.submit('继续')
            assert any(e['type']=='compaction/summary' for e in h.events)
            assert h.sent[-1]=='压缩后回复。'
            assert len(states(h.model.requests[-1]))==1
            await h.runtime.close();h.runtime=h.restart();await h.submit('重启后继续')
            assert h.sent[-1]=='重启后回复。'
            assert len(states(h.model.requests[-1]))==1
        finally:await h.close()
    asyncio.run(run())
