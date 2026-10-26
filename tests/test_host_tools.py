"""Exercise inherited native tools in both real hosts, including host policy."""
import json
import pytest
from settings import read_config,write_config
from support import call
from test_package import Installed,ARCHIVE,run_async
from test_desktop import Desktop,DESKTOP
from test_history import results,tool_text


@pytest.mark.parametrize('desktop',[False,True])
@pytest.mark.skipif(DESKTOP is None or not ARCHIVE.exists(),reason='Requires archive and official desktop')
@run_async
async def test_host_tools_inherit_execute_and_keep_channel_boundaries(tmp_path,desktop):
    h=Desktop(tmp_path) if desktop else Installed(tmp_path,[])
    try:
        # A host-added tool must work without adding an import/registration to
        # Telos. Its host-owned execution guard must still reject denied input.
        probe=tmp_path/'host-probe.mjs'
        probe.write_text("""
export const inject=['tools','workingDirectory'];
export function apply(ctx){
 ctx.tools.register({name:'host_probe',description:'Host inheritance regression probe',
  parameters:{type:'object',properties:{deny:{type:'boolean'}},additionalProperties:false},
  output:{schema:{type:'string'},render:(_args,value)=>[{type:'text',text:value}]},
  execute:(_args,exec)=>'HOST_NATIVE_PROBE '+ctx.workingDirectory.get(exec.agent.session)});
 ctx.tools.guard(exec=>exec.name==='host_probe'&&exec.arguments.deny?'HOST_POLICY_DENIED':undefined);
}
""")
        profile=h.profile if desktop else h.home/'profiles/telos'
        patch=profile/'cordis.patch.yml';rows=json.loads(patch.read_text())
        rows.append({'insert':[{'id':'host-probe','name':probe.as_uri()}]})
        patch.write_text(json.dumps(rows))
        if desktop:
            await h.start();await h.action('save',tools={'apple':False,'maps':False})
            config=read_config(h.home/'telos')
            config['wechat'].update(base_url=h.services.url,cdn_base_url=h.services.url)
            write_config(h.home/'telos',config)
            await h.action('login');await h.action('start')
        else:
            h.run('--telos-login');h.start()
        async def send(key):
            count=len(h.services.sent);h.services.push(key,'在当前工作目录测试文件操作与工具边界。')
            await h.wait(lambda:len(h.services.sent)>count)
        h.model.script=[call('probe',{},'host_probe'),
            call('write',{'file_path':'host-tool-check.txt','content':'NATIVE_INITIAL\n'},'write'),
            call('read',{'file_path':'host-tool-check.txt'},'read'),
            call('edit',{'file_path':'host-tool-check.txt','old_string':'NATIVE_INITIAL','new_string':'NATIVE_UPDATED'},'edit'),
            call('glob',{'pattern':'host-tool-check.txt'},'glob'),
            call('grep',{'pattern':'NATIVE_UPDATED','path':'host-tool-check.txt'},'grep'),
            call('jobs',{},'job_list'),'通用工具完成。']
        await send('native-tools')
        output=results(h.model.requests[-1])
        assert len(output)==7 and all(not r.get('is_error') for r in output),output
        assert 'HOST_NATIVE_PROBE' in tool_text(output[0])
        assert 'host-tool-check.txt' in tool_text(output[4])
        assert 'NATIVE_UPDATED' in tool_text(output[5])
        file=next((h.home/'telos/users').glob('*/host-tool-check.txt'))
        assert file.read_text()=='NATIVE_UPDATED\n'
        names=[t['name'] for t in h.model.requests[-1]['tools']]
        assert len(names)==len(set(names))
        assert {'write','edit','glob','grep','job_list','host_probe','web_search','web_fetch','skill','todo_write'}<=set(names)
        assert not {'present','ask_user_question','exit_plan_mode','working_directory','session_search','session_trace',
                    'session_event_trace','subagent','subagent_fork','workflow','create_goal'}&set(names)
        # A fabricated call to a hidden tool must fail, not merely be absent
        # from the model's schema. The inherited host guard must also run.
        h.model.script=[call('denied',{'deny':True},'host_probe'),
            call('other-session',{'session_id':'unrelated','seq':0},'session_event_read'),
            call('host-dialog',{'questions':[]},'ask_user_question'),'边界检查完成。']
        await send('boundaries')
        rejected=results(h.model.requests[-1])[-3:]
        assert all(r.get('is_error') for r in rejected),rejected
        assert 'HOST_POLICY_DENIED' in tool_text(rejected[0])
        assert 'TELOS_HISTORY_CURRENT_SESSION_ONLY' in tool_text(rejected[1])
        h.model.script=[call('background',{'command':'sleep 2','description':'Wait for native background completion',
                                         'run_in_background':True},'bash'),
                        '后台已启动。',call('finished-jobs',{},'job_list'),'后台已完成。']
        await send('background')
        await h.wait(lambda:'后台已完成。' in json.dumps(h.services.sent,ensure_ascii=False))
        assert not results(h.model.requests[-1])[-1].get('is_error')
        assert '(no background jobs)' not in tool_text(results(h.model.requests[-1])[-1])
        # Restart replays the native log through the existing outbox; it must
        # not send the background completion a second time.
        h.stop()
        if desktop:await h.start();await h.wait(lambda:h.get()['running'])
        else:h.start()
        h.model.script=[call('resumed',{'file_path':'host-tool-check.txt'},'read'),'重启后读取完成。']
        await send('resume')
        assert 'NATIVE_UPDATED' in tool_text(results(h.model.requests[-1])[-1])
        assert json.dumps(h.services.sent,ensure_ascii=False).count('后台已完成。')==1
        assert not h.services.apple.writes
    finally:await h.close()
