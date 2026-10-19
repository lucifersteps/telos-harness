"""Real DSH file loading, isolation, bounded writes and durable resume."""
import asyncio
from dataclasses import replace
import json
from pathlib import Path
from support import Harness,call


def profile_events(h):
    return [e for e in h.events if e['type']=='user/message'
            and e['data'].get('source',{}).get('kind')=='agent-instructions'
            and any(c['path'] in {'user.md','AGENTS.md'} for c in e['data']['source']['changes'])]


def test_memory_native_changes_tools_limits_and_resume(tmp_path):
    async def run():
        h=Harness(tmp_path,[])
        text='偏好：简洁回答。'
        try:
            h.model.script=[call('read',{'action':'read'},'user_memory'),
                            call('write',{'action':'write','content':text,'expected_content':''},'user_memory'),'已记住。']
            await h.submit('记住，回答尽量简洁')
            file=next((h.runtime.root/'users').glob('*/user.md'))
            assert file.read_text()==text
            assert '1375' in json.dumps(h.model.requests[-1])
            before=len(profile_events(h))
            # Dynamic runtime fields change without duplicating the native file context.
            context=h.runtime.business.context
            async def changing(session):
                value=await context(session)
                return {**value,'time':'new-time','pending':{'test':'preview'},'receipt':{'test':'saved'}}
            h.runtime.business.context=changing
            h.model.script=['继续。'];await h.submit('继续')
            assert len(profile_events(h))==before
            file.write_text(text)  # mtime changes; content does not.
            h.model.script=['继续。'];await h.submit('同样的内容')
            assert len(profile_events(h))==before
            h.runtime.business.context=context
            next_text='偏好：使用编号列表。'
            file.write_text(next_text)
            previous=h.model.requests[-1]['messages']
            h.model.script=['好。'];await h.submit('文件已更新')
            assert len(profile_events(h))==before+1
            assert next_text in json.dumps(h.model.requests[-1],ensure_ascii=False)
            assert h.model.requests[-1]['messages'][:len(previous)]==previous
            # Unicode limit, failed writes and stale updates leave the file intact.
            h.model.script=[call('too-long',{'action':'write','content':'字'*1376,'expected_content':next_text},'user_memory'),
                            call('stale',{'action':'write','content':'lost update','expected_content':text},'user_memory'),'未覆盖。']
            await h.submit('检查编辑限制')
            assert file.read_text()==next_text
            results=json.dumps([e for e in h.events if e['type']=='tool/result'])
            assert 'Character limit exceeded' in results and 'File changed' in results
            exact='😀'*1375
            h.model.script=[call('exact',{'action':'write','content':exact,'expected_content':next_text},'user_memory'),'完成。']
            await h.submit('边界字符测试');assert file.read_text()==exact
            h.model.script=[call('clear',{'action':'write','content':'','expected_content':exact},'user_memory'),'已清除。']
            await h.submit('忘掉已记录的资料');assert file.read_text()==''
            file.write_text(text);h.model.script=['收到。'];await h.submit('恢复资料')
            before=len(profile_events(h))
            await h.runtime.close();h.runtime=h.restart()
            h.model.script=['恢复成功。'];await h.submit('重启后继续')
            assert len(profile_events(h))==before
            assert text in json.dumps(h.model.requests[-1],ensure_ascii=False)
        finally:await h.close()
    asyncio.run(run())


def test_memory_user_and_tenant_isolation(tmp_path):
    async def run():
        h=Harness(tmp_path,[])
        try:
            h.model.script=[call('save',{'action':'write','content':'USER_A_PRIVATE','expected_content':''},'user_memory'),'ok']
            await h.submit('remember')
            original=h.ref
            h.ref=replace(original,user_id='other-user');h.session='wechat-other'
            h.model.script=[call('read-b',{'action':'read'},'user_memory'),'ok'];await h.submit('who am I')
            assert 'USER_A_PRIVATE' not in json.dumps(h.model.requests[-1])
            h.ref=replace(original,tenant_id='other-tenant');h.session='wechat-other-tenant'
            h.model.script=['ok'];await h.submit('different tenant')
            assert 'USER_A_PRIVATE' not in json.dumps(h.model.requests[-1])
            h.ref=original;h.session='wechat-same-user-new-conversation'
            h.model.script=['ok'];await h.submit('same user different conversation')
            assert 'USER_A_PRIVATE' in json.dumps(h.model.requests[-1])
            files=list((h.runtime.root/'users').glob('*/user.md'))
            assert len(files)==3
        finally:await h.close()
    asyncio.run(run())


def test_existing_session_keeps_history_when_moving_to_user_directory(tmp_path):
    async def run():
        h=Harness(tmp_path,['LEGACY_HISTORY_MARKER'])
        fixture=h.runtime.root/'harness/profiles/sdk-minimal/node_modules/telos-native-validation/index.mjs'
        fixture.write_text(fixture.read_text().replace('await mountUserMemory(scope,agent,memoryDirectory);','')
                           .replace('meta:{cwd:directory}','meta:{cwd:process.cwd()}'))
        try:
            await h.submit('old conversation')
            assert h.sent[-1]=='LEGACY_HISTORY_MARKER'
            await h.runtime.close();h.runtime=h.restart()
            h.model.script=[call('migrate',{'action':'write','content':'MIGRATED_PROFILE','expected_content':''},'user_memory'),'restored']
            await h.submit('continue existing history')
            assert 'LEGACY_HISTORY_MARKER' in json.dumps(h.model.requests[-1])
            assert next((h.runtime.root/'users').glob('*/user.md')).read_text()=='MIGRATED_PROFILE'
            from compression import zstd
            log=next((h.runtime.root/'harness/sessions').glob('*/*/session.v4.jsonl.zstd'))
            with zstd.open(log,'rt') as stream:events=[json.loads(line) for line in stream]
            assert any(e['type']=='working-directory/change' and '/users/' in e['data']['cwd'] for e in events)
        finally:await h.close()
    asyncio.run(run())
