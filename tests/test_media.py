"""真实微信加解密和本机格式转换；不使用个人媒体或外部模型。"""
import asyncio
import base64
import io
import struct
import wave
from pathlib import Path
import pytest
from cryptography.hazmat.primitives.ciphers import Cipher,algorithms,modes
from telos.adapters.wechat.config import WeChatConfig
from telos.adapters.wechat.media import create_wechat_media_resolver,extract_attachments
from telos.adapters.wechat.inbound import normalize_wechat_event
from telos.connectors.voice import to_wav,VoiceInputError,MAX_AUDIO_BYTES

def png(size=32,color=b'\xff\0\0'):
    import zlib
    def chunk(kind,data):return struct.pack('>I',len(data))+kind+data+struct.pack('>I',zlib.crc32(kind+data))
    return b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',size,size,8,2,0,0,0))+chunk(b'IDAT',zlib.compress((b'\0'+color*size)*size))+chunk(b'IEND',b'')
PNG=png()


def encrypted_item(data,kind=3,key='asset',**metadata):
    aes=b'1234567890abcdef';pad=16-len(data)%16;encryptor=Cipher(algorithms.AES(aes),modes.ECB()).encryptor()
    cipher=encryptor.update(data+bytes([pad])*pad)+encryptor.finalize()
    name={2:'image_item',3:'voice_item',4:'file_item'}[kind]
    return {'type':kind,name:{'media':{'encrypt_query_param':key,'aes_key':base64.b64encode(aes).decode()},**metadata}},cipher

def assert_wav(data):
    assert data[:4]==b'RIFF' and data[36:40]==b'data'
    with wave.open(io.BytesIO(data)) as f:
        assert (f.getnchannels(),f.getsampwidth(),f.getframerate())==(1,2,16000)
        assert 0<f.getnframes()<=120*16000

@pytest.mark.parametrize('rate',[8000,16000,48000])
def test_pcm_conversion_and_cleanup(tmp_path,rate):
    data=struct.pack('<h',4000)*(rate//2)
    assert_wav(asyncio.run(to_wav(data,{'encode_type':1,'sample_rate':rate},work_root=tmp_path)))
    assert not list(tmp_path.iterdir())

def test_silk_conversion_uses_codec_only(tmp_path):
    from codec_fixture import encode_silk
    source=tmp_path/'sample.pcm';target=tmp_path/'sample.silk';source.write_bytes(b'\0\0'*16000)
    encode_silk(source,target)
    assert_wav(asyncio.run(to_wav(target.read_bytes(),{},work_root=tmp_path/'work')))
    assert not list((tmp_path/'work').iterdir())

@pytest.mark.parametrize('data,metadata',[(b'',{}),(b'x'*(MAX_AUDIO_BYTES+1),{}),(b'x',{'playtime':120001}),
    (b'x',{}),(b'\x02#!SILK_V3\xff',{}),(b'xx',{'encode_type':1,'sample_rate':100}),
    (b'xx',{'encode_type':1,'bits_per_sample':8}),(b'RIFFbad',{})])
def test_bad_audio_is_bounded_and_cleans_temporary_files(tmp_path,data,metadata):
    with pytest.raises(VoiceInputError):asyncio.run(to_wav(data,metadata,work_root=tmp_path))
    assert not list(tmp_path.iterdir())

def test_encrypted_voice_native_callback_and_origin(tmp_path):
    async def run():
        item,cipher=encrypted_item(b'pcm-content');seen=[]
        async def transcribe(data,metadata):seen.append(data);return '确认'
        resolver=create_wechat_media_resolver(WeChatConfig(),media_dir=tmp_path,fetch_bytes_fn=lambda *a:cipher,voice_transcriber=transcribe)
        result=await resolver({'item_list':[item]})
        turn=normalize_wechat_event(result,account_id='test')
        assert seen==[b'pcm-content'] and '[Voice message received, transcribed below]' in turn.text and '确认' in turn.text
        assert 'text' not in item['voice_item'] # no mutation of raw input
    asyncio.run(run())

def test_quoted_image_is_decrypted_and_admitted(tmp_path):
    async def run():
        item,cipher=encrypted_item(PNG,kind=2)
        resolver=create_wechat_media_resolver(WeChatConfig(),media_dir=tmp_path,fetch_bytes_fn=lambda *a:cipher)
        result=await resolver({'item_list':[{'type':1,'text_item':{'text':'看这张图'},'ref_msg':{'message_item':item}}]})
        attachments=extract_attachments(result['item_list'])
        assert len(attachments)==1 and Path(attachments[0].uri).read_bytes()==PNG
    asyncio.run(run())

@pytest.mark.parametrize('failure',['download','decrypt','transcribe','empty'])
def test_voice_failure_keeps_supplement_without_inventing_content(tmp_path,failure):
    async def run():
        item,cipher=encrypted_item(b'audio')
        def fetch(*args):
            if failure=='download':raise OSError('sensitive-url')
            return b'badcipher' if failure=='decrypt' else cipher
        async def transcribe(*args):
            if failure=='transcribe':raise RuntimeError('sensitive-path')
            return ''
        resolver=create_wechat_media_resolver(WeChatConfig(),media_dir=tmp_path,fetch_bytes_fn=fetch,voice_transcriber=transcribe)
        result=await resolver({'item_list':[item,{'type':1,'text_item':{'text':'改成明天下午'}}]})
        turn=normalize_wechat_event(result,account_id='test')
        assert '内容未知' in turn.text and '改成明天下午' in turn.text and 'sensitive' not in str(result)
    asyncio.run(run())
