"""安装包中的微信下载/解密 → DSH 原生媒体 → 预览 → 确认 → 回执。"""
import asyncio
import base64
import json
import os
from pathlib import Path
import subprocess
import wave
import pytest
from test_package import Installed,run_async
from test_media import encrypted_item,PNG,png
from support import call,draft


def push_media(h,id,items):
    h.services.messages.append({'message_id':id,'from_user_id':'test','to_user_id':'isolated-bot','message_type':1,
        'context_token':'mock-context','item_list':items})

def asset(h,data,kind,key,**metadata):
    item,cipher=encrypted_item(data,kind,key,**metadata);h.services.assets[key]=cipher;return item

def text(value):return {'type':1,'text_item':{'text':value}}

@pytest.mark.parametrize('quoted',[False,True])
@run_async
async def test_installed_encrypted_image_preview_restart_confirm(tmp_path,quoted):
    h=Installed(tmp_path,[call('picture-preview',draft()),'请确认图中的日程。','已保存。','无待确认操作。'])
    try:
        h.run('--telos-login');h.start()
        item=asset(h,PNG,2,'picture');items=[item,text('把图片中的会议加入日历')]
        if quoted:items=[{**text('把引用图片中的会议加入日历'),'ref_msg':{'message_item':item}}]
        push_media(h,'picture',items);await h.wait(lambda:len(h.services.sent)>0)
        first=json.dumps(h.model.requests[0],ensure_ascii=False)
        assert base64.b64encode(PNG).decode() in first
        assert not h.services.apple.writes and h.services.downloads==['picture']
        assert not list((h.data/'media').iterdir())
        # The duplicate media id must not download or execute the model again.
        push_media(h,'picture',items);h.services.push('confirm','1')
        await h.wait(lambda:len(h.services.apple.writes)==1 and len(h.services.sent)>1)
        assert h.services.downloads==['picture']
        h.stop();h.start();h.services.push('again','1');await h.wait(lambda:len(h.services.sent)>2)
        assert len(h.services.apple.writes)==1
    finally:await h.close()

@pytest.mark.parametrize('partial',[False,True])
@run_async
async def test_installed_missing_image_cannot_confirm_or_read_local_file(tmp_path,partial):
    h=Installed(tmp_path,[call('preview',draft()),'请确认。',call('renewed',draft()),'图片不完整，请重新发送；原日程重新预览。','已保存。'])
    try:
        h.run('--telos-login');h.start();h.services.push('create','创建会议');await h.wait(lambda:len(h.services.sent)>0)
        private=tmp_path/'private.png';private.write_bytes(b'PRIVATE-FILE-CONTENT')
        items=[{'type':'2','image_item':{'url':str(private)}},text('1')]
        if partial:items.insert(0,asset(h,PNG,2,'good'))
        push_media(h,'bad-image',items);await h.wait(lambda:len(h.services.sent)>1)
        request=json.dumps(h.model.requests[-1],ensure_ascii=False)
        assert '缺失图片内容未知' in request and 'PRIVATE-FILE-CONTENT' not in request
        assert not h.services.apple.writes and not list((h.data/'media').iterdir())
        h.services.push('real-confirm','1');await h.wait(lambda:len(h.services.sent)>2)
        assert len(h.services.apple.writes)==1
    finally:await h.close()

@run_async
async def test_installed_unprepared_voice_keeps_supplement_and_pending_preview(tmp_path):
    h=Installed(tmp_path,[call('p',draft()),'请确认。',call('renewed',draft()),'语音未能识别，原日程重新预览。','已保存。'])
    try:
        h.run('--telos-login');h.start();h.services.push('request','创建会议');await h.wait(lambda:len(h.services.sent)>0)
        item=asset(h,b'\0\0'*16000,3,'voice',encode_type=1,sample_rate=16000)
        push_media(h,'voice',[item,text('1')]);await h.wait(lambda:len(h.services.sent)>1)
        assert '语音内容未知' in json.dumps(h.model.requests[-1],ensure_ascii=False)
        assert not h.services.apple.writes and not list((h.data/'media').iterdir())
        h.services.push('confirm','1');await h.wait(lambda:len(h.services.sent)>2)
        assert len(h.services.apple.writes)==1
    finally:await h.close()

@pytest.mark.skipif(not os.environ.get('TELOS_TEST_SPEECH_ROOT'),reason='需要准备好的 DSH 原生 SenseVoice 模型')
@run_async
async def test_installed_real_sensevoice_silk_preview_voice_not_confirmation_restart(tmp_path):
    import pilk
    h=Installed(tmp_path,[call('voice-preview',draft()),'请确认。',call('renewed',draft()),'请使用文字确认。',call('after-silence',draft()),'语音为空，请使用文字确认。','已保存。','没有待确认操作。'])
    try:
        patch=tmp_path/'speech.yml';patch.write_text('- id: speech-to-text-sensevoice\n  config:\n    dataRoot: '+json.dumps(os.environ['TELOS_TEST_SPEECH_ROOT'])+'\n')
        h.launch_extra=['--patch',str(patch)]
        prepared=h.run('--telos-prepare-voice');assert '已就绪' in prepared.stdout
        # Speech is generated from a synthetic request; microphone/personal recordings are never used.
        def spoken(phrase,name):
            wav=tmp_path/(name+'.wav');pcm=tmp_path/(name+'.pcm');silk=tmp_path/(name+'.silk')
            subprocess.run(['/usr/bin/say','-v','Tingting','-o',str(wav),'--file-format=WAVE','--data-format=LEI16@16000',phrase],check=True)
            with wave.open(str(wav)) as f:pcm.write_bytes(f.readframes(f.getnframes()))
            pilk.encode(str(pcm),str(silk),pcm_rate=16000,tencent=True)
            return asset(h,silk.read_bytes(),3,name)
        request=spoken('请创建明天下午三点的测试会议。','request')
        confirm=spoken('确认。确认。','approval')
        h.run('--telos-login');h.start();push_media(h,'spoken-request',[request]);await h.wait(lambda:len(h.services.sent)>0,timeout=80)
        model=json.dumps(h.model.requests[0],ensure_ascii=False)
        user_text=json.dumps(h.model.requests[0]['messages'][0]['content'],ensure_ascii=False)
        assert '[Voice message received, transcribed below]' in user_text and '明天' in user_text and '会议' in user_text and ('3点' in user_text or '三点' in user_text)
        assert not h.services.apple.writes and not list((h.data/'media').iterdir())
        push_media(h,'spoken-request',[request]);push_media(h,'spoken-approval',[confirm]);await h.wait(lambda:len(h.services.sent)>1,timeout=80)
        assert '确认' in json.dumps(h.model.requests[-1],ensure_ascii=False) and not h.services.apple.writes
        assert h.services.downloads.count('request')==1
        silence=asset(h,b'\0\0'*16000,3,'silence',encode_type=1,sample_rate=16000)
        push_media(h,'silence',[silence]);await h.wait(lambda:len(h.services.sent)>2,timeout=80)
        assert '语音内容未知' in json.dumps(h.model.requests[-1],ensure_ascii=False) and not h.services.apple.writes
        h.stop();assert '已就绪' in h.run('--telos-prepare-voice').stdout
        h.start();h.services.push('written-approval','1');await h.wait(lambda:len(h.services.sent)>3)
        h.services.push('written-approval-duplicate','1');await h.wait(lambda:len(h.services.sent)>4)
        assert len(h.services.apple.writes)==1 and not list((h.data/'media').iterdir())
        # No Whisper, recording UI or web server was enabled in this profile.
        config=(h.home/'profiles/sdk-minimal/cordis.yml').read_text()
        assert 'voice-input' not in config and 'webui' not in config
    finally:await h.close()

@pytest.mark.skipif(not os.environ.get('TELOS_MEDIA_LIVE'),reason='真实视觉模型测试须显式启用')
@run_async
async def test_real_model_reads_native_image(tmp_path):
    from test_live import connection
    import sqlite3
    h=Installed(tmp_path,[],connection=connection())
    try:
        h.run('--telos-login');h.start()
        push_media(h,'color',[asset(h,png(512,b'\0\0\xff'),2,'blue-picture'),text('这是一张合成测试图片。图片主体是什么颜色？只回答颜色，不调用工具。')])
        await h.wait(lambda:len(h.services.sent)>0,timeout=120)
        with sqlite3.connect(h.data/'business.sqlite') as db:reply=db.execute('SELECT reply FROM messages').fetchone()[0]
        assert '蓝' in reply or 'blue' in reply.lower(),reply
        assert not h.services.apple.writes and not list((h.data/'media').iterdir())
    finally:await h.close()
