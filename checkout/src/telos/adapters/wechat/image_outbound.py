"""Bounded image download and iLink encrypted upload. No user-selected recipient."""
import asyncio
import tempfile
from pathlib import Path
import base64
import hashlib
import json
from urllib import request as urllib_request
import ipaddress
import secrets
from urllib.parse import urlencode, urlsplit

import aiohttp
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from telos.adapters.delivery import compact_json
from .outbound import _headers, _is_stale_session

MAX_IMAGE_BYTES = 10 * 1024 * 1024


def public_url(value):
    if not isinstance(value, str) or len(value) > 8192:
        raise ValueError('Expected an HTTP(S) URL')
    p = urlsplit(value)
    if p.scheme not in ('https', 'http') or not p.hostname or p.username or p.password:
        raise ValueError('Expected a public HTTP(S) URL without credentials')
    try:
        ip = ipaddress.ip_address(p.hostname)
    except ValueError:
        if p.hostname.lower() == 'localhost' or p.hostname.lower().endswith(('.localhost', '.local')):
            raise ValueError('Private addresses are not allowed')
    else:
        if not ip.is_global: raise ValueError('Private addresses are not allowed')
    return value


class PublicResolver(aiohttp.resolver.ThreadedResolver):
    async def resolve(self, host, port=0, family=0):
        records = await super().resolve(host, port, family)
        # Clash TUN fake-IP range is used on Macs. Permit it only for known
        # image CDNs, never arbitrary user-supplied hosts or private addresses.
        trusted=host.lower().endswith(('.xhscdn.com','.cdn.weixin.qq.com'))
        def allowed(address):
            ip=ipaddress.ip_address(address)
            return ip.is_global or (trusted and ip.version==4 and ip in ipaddress.ip_network('198.18.0.0/15'))
        if any(not allowed(r['host']) for r in records):
            raise ValueError('Private addresses are not allowed')
        return records


def validate_image(data):
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise ValueError('Image must be between 1 byte and 10 MiB')
    if data.startswith(b'\x89PNG\r\n\x1a\n') or data.startswith(b'\xff\xd8\xff'):
        return
    raise ValueError('Expected a PNG or JPEG image')


async def normalize_image(data):
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        # macOS ImageIO via sips keeps this integration dependency-free.
        with tempfile.TemporaryDirectory(prefix='telos-cover-') as directory:
            source=Path(directory)/'cover.webp';target=Path(directory)/'cover.png'
            source.write_bytes(data)
            proc=await asyncio.create_subprocess_exec('/usr/bin/sips','-s','format','png',str(source),'--out',str(target),
                stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.DEVNULL)
            try:
                await asyncio.wait_for(proc.wait(),20)
            except BaseException:
                if proc.returncode is None:proc.kill()
                await proc.wait();raise
            if proc.returncode or not target.exists():raise ValueError('Could not decode WebP cover')
            if target.stat().st_size>MAX_IMAGE_BYTES:raise ValueError('Converted image exceeds 10 MiB')
            data=target.read_bytes()
    validate_image(data)
    return bytes(data)


async def download_image(url):
    public_url(url)
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(resolver=PublicResolver()),
            timeout=aiohttp.ClientTimeout(total=45), trust_env=False) as client:
        for _ in range(5):
            public_url(url)
            async with client.get(url, allow_redirects=False) as response:
                if response.status in (301,302,303,307,308):
                    from urllib.parse import urljoin
                    url = urljoin(url, response.headers.get('Location', ''))
                    continue
                response.raise_for_status()
                if response.content_length and response.content_length > MAX_IMAGE_BYTES:
                    raise ValueError('Image exceeds 10 MiB')
                data = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    data.extend(chunk)
                    if len(data) > MAX_IMAGE_BYTES: raise ValueError('Image exceeds 10 MiB')
                return await normalize_image(data)
    raise ValueError('Too many image redirects')


class ImageAPIRejected(RuntimeError):
    def __init__(self, ret, errcode, errmsg=''):
        self.prepare_failed=(ret == -2 and str(errmsg).strip().lower() == 'prepare failed')
        self.stale_context=_is_stale_session(ret,errcode,errmsg)
        super().__init__(f'WeChat rejected request: ret={ret} errcode={errcode} stale_context={self.stale_context}')


def post_api(url, body, headers):
    request=urllib_request.Request(url,data=compact_json(body).encode(),headers=headers,method='POST')
    with urllib_request.urlopen(request,timeout=30) as response:
        raw=response.read(1024*1024)
    # iLink sendmessage may acknowledge with HTTP 200 and an empty body.
    result=json.loads(raw) if raw.strip() else {}
    if not isinstance(result,dict):raise RuntimeError('Invalid WeChat API response')
    return result


async def api(config, endpoint, payload):
    body = {**payload, 'base_info': {'channel_version': '2.2.0'}}
    result = await asyncio.to_thread(post_api,config.base_url.rstrip('/')+'/'+endpoint,body,
                                    _headers(config.token, compact_json(body)))
    if result.get('ret',0) not in (0,None) or result.get('errcode',0) not in (0,None):
        raise ImageAPIRejected(result.get('ret'),result.get('errcode'),result.get('errmsg') or result.get('msg') or 'unknown error')
    return result


async def upload_image(config, recipient, data):
    validate_image(data)
    key = secrets.token_bytes(16)
    padder = padding.PKCS7(128).padder()
    padded = padder.update(data)+padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    encrypted = encryptor.update(padded)+encryptor.finalize()
    filekey = secrets.token_hex(16)
    result = await api(config, 'ilink/bot/getuploadurl', {
        'filekey':filekey, 'media_type':1, 'to_user_id':recipient,
        'rawsize':len(data), 'rawfilemd5':hashlib.md5(data).hexdigest(),
        'filesize':len(encrypted), 'no_need_thumb':True, 'aeskey':key.hex()})
    url = result.get('upload_full_url')
    if not url and result.get('upload_param'):
        url = config.cdn_base_url.rstrip('/')+'/upload?'+urlencode({
            'encrypted_query_param':result['upload_param'], 'filekey':filekey})
    if not url: raise RuntimeError('WeChat did not return an upload URL')
    public_url(url)
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(resolver=PublicResolver()),
            timeout=aiohttp.ClientTimeout(total=60), trust_env=False) as client:
        async with client.post(url,data=encrypted,headers={'Content-Type':'application/octet-stream'},allow_redirects=False) as response:
            if response.status != 200: raise RuntimeError(f'WeChat image upload failed: HTTP {response.status}')
            param=response.headers.get('x-encrypted-param')
            if not param: raise RuntimeError('WeChat upload returned no download reference')
    return {'type':2,'image_item':{'media':{'encrypt_query_param':param,
        'aes_key':base64.b64encode(key.hex().encode()).decode(),'encrypt_type':1},'mid_size':len(encrypted)}}


async def send_item(config, recipient, context_token, item, client_id):
    message={'from_user_id':'','to_user_id':recipient,'client_id':client_id,
             'message_type':2,'message_state':2,'item_list':[item]}
    if context_token:message['context_token']=context_token
    try:
        return await api(config,'ilink/bot/sendmessage',{'msg':message})
    except ImageAPIRejected as error:
        # Match the established text channel's stale-context recovery. Retry
        # only an explicit rejection, never a timeout or ambiguous outcome.
        if not context_token or not error.stale_context:raise
        message.pop('context_token')
        result=await api(config,'ilink/bot/sendmessage',{'msg':message})
        return {**result,'context_reset':True}
