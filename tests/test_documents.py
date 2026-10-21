"""Synthetic originals + native skills/runtime/bash in actual CLI and Desktop hosts."""
import asyncio
import json
import os
from pathlib import Path
import shlex
import subprocess
import pytest
from documents import admit_document, document_directory
from support import Harness, call, draft
from test_history import results, tool_text
from test_media import encrypted_item
from test_package import Installed, ARCHIVE, run_async
from test_package_media import asset, push_media, text
from test_desktop import Desktop, DESKTOP
from settings import read_config, write_config
from runtime import ROOT
from telos.adapters.wechat.config import WeChatConfig
from telos.adapters.wechat.media import create_wechat_media_resolver, extract_attachments

FIXTURES=ROOT/'tests/fixtures/documents'
PRIMARY=os.environ.get('TELOS_TEST_PRIMARY_RUNTIME')
if not PRIMARY and DESKTOP:PRIMARY=str(DESKTOP/'Contents/Resources/runtime/primary-runtime')


def test_original_storage_and_name_is_not_path(tmp_path):
    async def run():
        source=FIXTURES/'meeting.docx';directory=document_directory(tmp_path,'wechat-test')
        result=await admit_document(source,'../../unsafe;name.docx',directory)
        target=Path(result['path'])
        assert target.parent==directory and target.suffix=='.docx'
        assert target.read_bytes()==source.read_bytes() and target.stat().st_mode & 0o777==0o600
        assert await admit_document(source,'../../unsafe;name.docx',directory)==result
    asyncio.run(run())


@pytest.mark.parametrize('quoted',[False,True])
def test_encrypted_file_download_and_bad_uri(tmp_path,quoted):
    async def run():
        item,cipher=encrypted_item(b'private attachment',4,name='../../会议.txt')
        items=[{**text('处理这个'), 'ref_msg':{'message_item':item}}] if quoted else [item]
        resolver=create_wechat_media_resolver(WeChatConfig(),media_dir=tmp_path,fetch_bytes_fn=lambda *a:cipher)
        result=await resolver({'item_list':items});attachment=extract_attachments(result['item_list'])[0]
        assert Path(attachment.uri).parent==tmp_path and Path(attachment.uri).read_bytes()==b'private attachment'
        assert 'url' not in item['file_item']
        for uri in ['/etc/passwd','http://localhost/private','https://example.com/private']:
            result=await resolver({'item_list':[{'type':4,'file_item':{'name':'a.txt','url':uri}}]})
            assert not extract_attachments(result['item_list'])[0].uri
    asyncio.run(run())


def test_system_pdfkit_pages_limits_and_errors():
    command=['/usr/bin/osascript','-l','JavaScript',str(ROOT/'plugin/document-skills/telos-pdf/read.js')]
    result=subprocess.run([*command,str(FIXTURES/'meeting.pdf'),'1','2'],capture_output=True,text=True,check=True)
    parsed=json.loads(result.stdout)
    assert parsed['totalPages']==2 and 'MEETING' in parsed['pages'][0]['text'] and parsed['pages'][1]['noText']
    for filename,first,last in [('locked.pdf','1','1'),('meeting.docx','1','1'),('meeting.pdf','1','100'),('meeting.pdf','3','3')]:
        assert subprocess.run([*command,str(FIXTURES/filename),first,last],capture_output=True).returncode!=0


def attachment(request):
    for message in reversed(request['messages']):
        if not isinstance(message.get('content'),list):continue
        for block in message['content']:
            value=block.get('text','')
            if '[微信文件附件原件' in value:return json.loads(value.split('\n')[-1])[0]
    raise AssertionError('Missing original metadata')


def script_for(suffix,skill_path,done='文件读取完成。'):
    name={'.docx':'office-docx','.xlsx':'office-xlsx','.pdf':'telos-pdf'}[suffix]
    def execute(request):
        original=attachment(request);assert 'path' in original,original
        if suffix=='.pdf':
            assert 'PDFKit' in tool_text(results(request)[-1])
            command=shlex.join(['/usr/bin/osascript','-l','JavaScript',str(skill_path),original['path'],'1','2'])
        else:
            runtime=json.loads(tool_text(results(request)[-1]))
            code=("import sys; from docx import Document; d=Document(sys.argv[1]); print([p.text for p in d.paragraphs]); print([[c.text for c in r.cells] for t in d.tables for r in t.rows])"
                  if suffix=='.docx' else "import sys; from openpyxl import load_workbook; w=load_workbook(sys.argv[1],read_only=True,data_only=False); print([(s.title,list(s.values)) for s in w]); w.close()")
            command=shlex.join([runtime['python'],'-B','-c',code,original['path']])
        return call('inspect',{'description':'Inspect received document content','command':command,'timeoutMs':30000},'bash')
    steps=[call('skill',{'name':name},'skill')]
    if suffix!='.pdf':steps.append(call('dependencies',{},'load_workspace_dependencies'))
    return steps+[execute,done]


def assert_inspected(request,suffix):
    result=results(request)[-1];value=tool_text(result)
    assert not result.get('is_error') and 'MEETING' in value and '14:00-15:00' in value,value
    if suffix=='.xlsx':assert '=1+2' in value
    if suffix=='.pdf':assert '"noText":true' in value
    names=[t.get('name') or t['function']['name'] for t in request['tools']]
    for name in ['skill','bash','read','read_image']:assert names.count(name)==1
    assert not {'job_output','job_kill','write','edit'} & set(names)


@pytest.mark.parametrize('suffix',['.pdf','.docx','.xlsx'])
@run_async
async def test_native_document_skills(tmp_path,monkeypatch,suffix):
    if suffix!='.pdf' and not PRIMARY:pytest.skip('Set TELOS_TEST_PRIMARY_RUNTIME')
    if PRIMARY:monkeypatch.setenv('DSH_PRIMARY_RUNTIME',PRIMARY)
    h=Harness(tmp_path,['准备接收。'])
    try:
        await h.submit('准备接收文件')
        profile=await h.runtime.route('/memory-profile',{'session':h.session})
        doc=await admit_document(FIXTURES/('meeting'+suffix),'meeting'+suffix,document_directory(profile['directory'],h.session))
        h.model.script=script_for(suffix,ROOT/'plugin/document-skills/telos-pdf/read.js')
        await h.submit('[微信文件附件原件]\n'+json.dumps([doc]))
        assert_inspected(h.model.requests[-1],suffix)
        assert not h.native.writes
    finally:await h.close()


@pytest.mark.parametrize('suffix',['.pdf','.docx','.xlsx'])
@run_async
async def test_installed_document_and_restart(tmp_path,suffix):
    if suffix!='.pdf' and not PRIMARY:pytest.skip('Set TELOS_TEST_PRIMARY_RUNTIME')
    h=Installed(tmp_path,[])
    if PRIMARY:h.env['DSH_PRIMARY_RUNTIME']=PRIMARY
    helper=h.home/'profiles/telos/node_modules/telos-harness/document-skills/telos-pdf/read.js'
    try:
        h.model.script=script_for(suffix,helper)
        h.run('--telos-login');h.start()
        source=FIXTURES/('meeting'+suffix);item=asset(h,source.read_bytes(),4,'document',name=source.name)
        push_media(h,'document',[item,text('读取文件里的会议安排')])
        await h.wait(lambda:len(h.services.sent)>0,timeout=45)
        assert_inspected(h.model.requests[-1],suffix)
        saved=next((h.data/'users').glob('*/documents/*/*'+suffix))
        assert saved.read_bytes()==source.read_bytes() and not list((h.data/'media').iterdir())
        assert not h.services.apple.writes
        push_media(h,'document',[item]);h.stop();h.start()
        h.model.script=script_for(suffix,helper)
        h.services.push('resume','再看之前文件');await h.wait(lambda:len(h.services.sent)>1,timeout=45)
        assert_inspected(h.model.requests[-1],suffix)
        assert h.services.downloads==['document']
        h.model.script=[call('plan',draft()),'请确认。','已保存。']
        h.services.push('plan','把会议加入日历');await h.wait(lambda:len(h.services.sent)>2)
        assert not h.services.apple.writes
        h.services.push('confirm','1');await h.wait(lambda:len(h.services.sent)>3)
        assert len(h.services.apple.writes)==1
    finally:await h.close()


@run_async
async def test_failed_file_plus_one_is_not_confirmation(tmp_path):
    h=Installed(tmp_path,[call('preview',draft()),'请确认。','文件下载失败。'])
    try:
        h.run('--telos-login');h.start();h.services.push('preview','创建会议');await h.wait(lambda:len(h.services.sent)>0)
        push_media(h,'bad',[{'type':4,'file_item':{'name':'a.txt','url':'/etc/passwd'}},text('1')])
        await h.wait(lambda:len(h.services.sent)>1)
        assert not h.services.apple.writes and '内容未知' in json.dumps(h.model.requests[-1],ensure_ascii=False)
    finally:await h.close()


@pytest.mark.skipif(DESKTOP is None or not ARCHIVE.exists(),reason='Requires Desktop and built archive')
@pytest.mark.parametrize('suffix',['.pdf','.docx','.xlsx'])
@run_async
async def test_desktop_document(tmp_path,suffix):
    d=Desktop(tmp_path)
    helper=d.profile/'node_modules/telos-harness/document-skills/telos-pdf/read.js'
    d.model.script=script_for(suffix,helper)
    try:
        await d.start();await d.action('save',tools={'apple':False,'maps':False})
        root=d.home/'telos';config=read_config(root)
        config['wechat'].update(base_url=d.services.url,cdn_base_url=d.services.url);write_config(root,config)
        await d.action('login');await d.action('start')
        source=FIXTURES/('meeting'+suffix);item=asset(d,source.read_bytes(),4,'document',name=source.name)
        push_media(d,'document',[{**text('读取引用文件'),'ref_msg':{'message_item':item}}])
        await d.wait(lambda:len(d.services.sent)>0,timeout=45)
        assert_inspected(d.model.requests[-1],suffix)
        assert d.services.downloads==['document'] and not list((root/'media').iterdir())
    finally:await d.close()
