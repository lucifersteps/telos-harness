"""微信音频格式转换；识别、模型准备与推理由 DSH 原生服务负责。"""
from __future__ import annotations
import asyncio
import io
from pathlib import Path
import resource
import struct
import sys
import tempfile
import wave

MAX_AUDIO_BYTES=16*1024*1024
MAX_SECONDS=120
MAX_WAV_BYTES=MAX_SECONDS*16000*2+44

class VoiceInputError(ValueError):
    """可展示给用户的媒体错误，不携带下载 URL、凭据或音频内容。"""


def _limit_output():resource.setrlimit(resource.RLIMIT_FSIZE,(MAX_WAV_BYTES+65536,MAX_WAV_BYTES+65536))

async def run_local_process(*args,timeout=30):
    process=await asyncio.create_subprocess_exec(*map(str,args),stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.DEVNULL,preexec_fn=_limit_output)
    try:
        await asyncio.wait_for(process.wait(),timeout)
        if process.returncode:raise VoiceInputError('这条语音未能解码，请重新发送或改用文字。')
    except TimeoutError as error:raise VoiceInputError('语音格式转换超时，请缩短后重新发送。') from error
    finally:
        if process.returncode is None:
            process.kill();await process.wait()


def _write_wav(path,pcm,rate):
    if len(pcm)%2 or len(pcm)>MAX_SECONDS*rate*2:raise VoiceInputError('语音格式无效或超过 120 秒。')
    with wave.open(str(path),'wb') as f:
        f.setnchannels(1);f.setsampwidth(2);f.setframerate(rate);f.writeframes(pcm)


def decode_silk(source,destination):
    data=Path(source).read_bytes()
    header=b'\x02#!SILK_V3' if data.startswith(b'\x02') else b'#!SILK_V3'
    if not data.startswith(header):raise VoiceInputError('SILK 格式无效。')
    offset,frames=len(header),0
    while offset<len(data):
        if offset+2>len(data):raise VoiceInputError('SILK 帧不完整。')
        size=struct.unpack_from('<h',data,offset)[0];offset+=2
        if size==-1:break
        if size<0 or offset+size>len(data):raise VoiceInputError('SILK 帧无效。')
        frames+=1
        if frames>MAX_SECONDS*50:raise VoiceInputError('语音超过 120 秒。')
        offset+=size
    import pilk
    pilk.decode(str(source),str(destination),pcm_rate=16000)


async def to_wav(data,metadata,*,work_root):
    if not data or len(data)>MAX_AUDIO_BYTES:raise VoiceInputError('语音为空或超过 16 MiB。')
    if int(metadata.get('playtime') or 0)>MAX_SECONDS*1000:raise VoiceInputError('语音超过 120 秒，请分段发送。')
    Path(work_root).mkdir(parents=True,exist_ok=True,mode=0o700)
    with tempfile.TemporaryDirectory(prefix='audio-',dir=work_root) as directory:
        work=Path(directory);source=work/'input.audio';source.write_bytes(data)
        if data.startswith((b'#!SILK_V3',b'\x02#!SILK_V3')):
            entry=[] if getattr(sys,'frozen',False) else [Path(__file__).resolve().parents[4]/'sidecar.py']
            await run_local_process(sys.executable,*entry,'decode-silk',source,work/'decoded.pcm')
            source=work/'input.wav';_write_wav(source,(work/'decoded.pcm').read_bytes(),16000)
        elif int(metadata.get('encode_type') or 0)==1 and not data.startswith(b'RIFF'):
            rate=int(metadata.get('sample_rate') or 16000)
            if rate not in (8000,12000,16000,24000,32000,44100,48000) or int(metadata.get('bits_per_sample') or 16)!=16:
                raise VoiceInputError('不支持该 PCM 采样格式。')
            source=work/'input.wav';_write_wav(source,data,rate)
        elif not (data.startswith((b'RIFF',b'ID3',b'OggS',b'fLaC',b'#!AMR')) or data[:2] in (b'\xff\xfb',b'\xff\xf3',b'\xff\xf2')):
            raise VoiceInputError('不支持这条语音的音频格式。')
        target=work/'converted.wav'
        await run_local_process('/usr/bin/afconvert','-f','WAVE','-d','LEI16@16000','-c','1',source,target)
        # Normalize CoreAudio's ancillary chunks to the canonical WAV accepted by DSH.
        try:
            with wave.open(str(target),'rb') as f:
                if f.getnchannels()!=1 or f.getsampwidth()!=2 or f.getframerate()!=16000:raise ValueError()
                if f.getnframes()>MAX_SECONDS*16000:raise VoiceInputError('语音超过 120 秒，请分段发送。')
                pcm=f.readframes(f.getnframes())
            out=io.BytesIO()
            with wave.open(out,'wb') as f:
                f.setnchannels(1);f.setsampwidth(2);f.setframerate(16000);f.writeframes(pcm)
            return out.getvalue()
        except (wave.Error,ValueError) as error:
            if isinstance(error,VoiceInputError):raise
            raise VoiceInputError('语音格式转换失败。') from error
