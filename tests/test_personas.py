import json
import pytest
from personas import persona_text, selection, system_prompt, ROOT, MAX_BYTES
from settings import read_config
from support import call, draft
from test_package import ARCHIVE, Installed, run_async


def system_text(request):
    # DeepSeek Messages keeps the original system header for caching and appends
    # native system updates on resume. The last full prompt is authoritative.
    snapshots=[request.get('system','')]
    snapshots.extend(m['content'] for m in request['messages'] if m['role']=='system'
                     and '个人 GTD 助手' in json.dumps(m['content'],ensure_ascii=False))
    return json.dumps(snapshots[-1],ensure_ascii=False)


def test_legacy_default_and_bundled_persona_keep_business_rules():
    rules=(ROOT/'personal_prompt.md').read_text()
    assert system_prompt({}).startswith(rules)
    assert 'Apple 关闭；百度地图 关闭' in system_prompt({'tools':{'apple':False,'maps':False}})
    prompt=system_prompt({'persona':'liman'})
    assert '你叫黎曼' in prompt
    assert rules in prompt and '以业务规则为准' in prompt


@pytest.mark.parametrize('config',[
    {'persona':'../liman'}, {'persona':None}, {'persona':[]},
    {'persona':'custom'}, {'persona':'custom','persona_file':42},
    {'persona':'custom','persona_file':'relative.md'},
])
def test_invalid_selection(config):
    with pytest.raises(ValueError):selection(config)


@pytest.mark.parametrize('content',[b'',b'\xff',b'\0',b'x'*(MAX_BYTES+1)])
def test_invalid_file_content(tmp_path,content):
    path=tmp_path/'persona.md';path.write_bytes(content)
    with pytest.raises(ValueError):persona_text({'persona':'custom','persona_file':str(path)})


@pytest.mark.skipif(not ARCHIVE.exists(),reason='先构建插件归档')
@run_async
async def test_packaged_persona_switch_resume_confirmation_and_default(tmp_path):
    h=Installed(tmp_path,[call('bad',{'action':'batch','operations':[]}),call('draft',draft()),
        '请查看预览。','已保存。','没有新的操作。','你好。'])
    try:
        # User-selectable built-in, persisted by the same CLI/config path as desktop.
        h.run(['--telos-persona','liman']);h.run('--telos-setup')
        assert read_config(h.data)['persona']=='liman'
        status=h.run('--telos-status').stdout
        assert 'liman' in status and 'persona_file' in status
        h.run('--telos-login');h.start();h.services.push('draft','创建测试会议')
        await h.wait(lambda:len(h.services.sent)==1)
        assert len(h.model.requests)==3 and not h.services.apple.writes
        assert all('你叫黎曼' in system_text(r) for r in h.model.requests)
        # No settings mutation while processing: same existing instance lock.
        before=(h.data/'config.json').read_bytes()
        failure=h.run(['--telos-persona','default'],ok=False)
        assert failure.returncode!=0 and (h.data/'config.json').read_bytes()==before
        h.stop()
        path=tmp_path/'my persona.md';path.write_text('你叫回归助手。CUSTOM_PERSONA_MARKER。')
        h.run(['--telos-persona-file',str(path)])
        assert read_config(h.data)['persona_file']==str(path)
        h.start();h.services.push('confirm','1')
        await h.wait(lambda:len(h.services.sent)==2)
        assert len(h.services.apple.writes)==1
        current=system_text(h.model.requests[-1])
        assert 'CUSTOM_PERSONA_MARKER' in current and '你叫黎曼' not in current
        assert '创建测试会议' in json.dumps(h.model.requests[-1],ensure_ascii=False)
        h.services.push('again','1');await h.wait(lambda:len(h.services.sent)==3)
        assert len(h.services.apple.writes)==1
        h.stop();h.run(['--telos-persona','default'])
        assert 'persona_file' not in read_config(h.data)
        h.start();h.services.push('plain','你好');await h.wait(lambda:len(h.services.sent)==4)
        current=system_text(h.model.requests[-1])
        assert 'CUSTOM_PERSONA_MARKER' not in current and '你叫黎曼' not in current
        assert '个人 GTD 助手' in current and len(h.services.apple.writes)==1
    finally:await h.close()


@pytest.mark.skipif(not ARCHIVE.exists(),reason='先构建插件归档')
@run_async
async def test_packaged_invalid_persona_never_overwrites_config_or_falls_back(tmp_path):
    h=Installed(tmp_path,[])
    try:
        h.run(['--telos-persona','liman'])
        before=(h.data/'config.json').read_bytes()
        for args in [['--telos-persona','unknown'],['--telos-persona-file',str(tmp_path/'missing.md')]]:
            result=h.run(args,ok=False)
            assert result.returncode!=0 and (h.data/'config.json').read_bytes()==before
        custom=tmp_path/'custom.md';custom.write_text('这是自定义人设。')
        h.run(['--telos-persona-file',str(custom)])
        custom.unlink()
        config=(h.data/'config.json').read_bytes()
        result=h.run(None,ok=False)
        assert result.returncode!=0 and '人设文件' in result.stderr
        assert not h.services.polls and not h.model.requests
        assert (h.data/'config.json').read_bytes()==config
        # Switching to a bundled option recovers even when the custom file disappeared.
        h.run(['--telos-persona','liman'])
    finally:await h.close()
