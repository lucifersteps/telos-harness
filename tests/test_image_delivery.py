import asyncio
import json
import sqlite3
from types import SimpleNamespace
import pytest
import image_delivery as delivery
from telos.adapters.wechat import image_outbound as wire
from support import Harness,call

PNG=b'\x89PNG\r\n\x1a\n'+b'fixture'

@pytest.mark.parametrize('url',['file:///etc/passwd','http://127.0.0.1/a','http://[::1]/','http://169.254.169.254/','https://user:secret@example.com/a','http://localhost/a'])
def test_private_sources_rejected(url):
    with pytest.raises(ValueError):wire.public_url(url)

def test_image_validation():
    for data in [b'',b'<html>error</html>',PNG+b'x'*wire.MAX_IMAGE_BYTES]:
        with pytest.raises(ValueError):wire.validate_image(data)
    wire.validate_image(PNG)

def fake_channel(tmp_path):
    ref=SimpleNamespace(chat_id='current-user')
    def known(session):
        if session!='current-session':raise ValueError('Unknown WeChat conversation')
        return ref
    return SimpleNamespace(known_ref=known,delivery_locks={},
        business=SimpleNamespace(db=lambda:sqlite3.connect(tmp_path/'receipts.db')),
        adapter=SimpleNamespace(config=object(),_context_token_for_ref=lambda ref:'context'))

@pytest.mark.parametrize('fail',[None,1,2])
def test_order_receipts_and_retry_safety(tmp_path,monkeypatch,fail):
    sent=[]
    async def download(url):return PNG
    async def upload(*args):return {'type':2,'image_item':{'media':{'encrypt_query_param':'test'}}}
    async def send(config,to,context,item,id):
        sent.append((to,context,item,id))
        if len(sent)==fail:raise TimeoutError()
        return {'ret':0}
    monkeypatch.setattr(delivery,'download_image',download)
    monkeypatch.setattr(delivery,'upload_image',upload)
    monkeypatch.setattr(delivery,'send_item',send)
    async def run():
        c=fake_channel(tmp_path)
        data={'session':'current-session','id':'call1','args':{'image_url':'https://example.com/a.jpg','title':'标题','summary':'摘要','link_url':'https://www.xiaohongshu.com/explore/id?xsec_token=preserve'}}
        result=await delivery.share_image(c,data)
        assert result['status']=={None:'sent',1:'unknown',2:'partial'}[fail]
        assert sent[0][2]['type']==2
        if fail!=1:assert sent[1][2]['text_item']['text']=='标题\n摘要\nhttps://www.xiaohongshu.com/explore/id?xsec_token=preserve'
        assert all(to=='current-user' and context=='context' for to,context,_,_ in sent)
        # A new process sees persisted receipts and does not resend either part.
        before=len(sent)
        assert await delivery.share_image(fake_channel(tmp_path),data)==result
        assert len(sent)==before
        with pytest.raises(ValueError):await delivery.share_image(c,{**data,'session':'other'})
        with pytest.raises(ValueError):await delivery.share_image(c,{**data,'args':{**data['args'],'title':'changed'}})
    asyncio.run(run())

def test_host_tool_calls_bound_channel(tmp_path,monkeypatch):
    sent=[]
    async def download(url):return PNG
    async def upload(*args):return {'type':2,'image_item':{}}
    async def send(config,to,context,item,id):sent.append(item);return {'ret':0}
    monkeypatch.setattr(delivery,'download_image',download)
    monkeypatch.setattr(delivery,'upload_image',upload)
    monkeypatch.setattr(delivery,'send_item',send)
    async def run():
        h=Harness(tmp_path,[call('cover',{'image_url':'https://example.com/image.jpg','title':'测试笔记','summary':'摘要','link_url':'https://www.xiaohongshu.com/explore/test'},'send_wechat_image'),'分享完成。'])
        try:
            await h.submit('把这篇笔记的封面和链接发给我')
            assert len(sent)==2 and sent[0]['type']==2 and sent[1]['type']==1
            from test_history import results,tool_text
            assert '"status":"sent"' in tool_text(results(h.model.requests[-1])[-1]).replace(' ','')
            assert 'send_wechat_image' in [t['name'] for t in h.model.requests[0]['tools']]
        finally:await h.close()
    asyncio.run(run())

def test_upload_wire_encryption_and_send_schema(monkeypatch):
    import base64,hashlib
    from cryptography.hazmat.primitives.ciphers import Cipher,algorithms,modes
    from cryptography.hazmat.primitives import padding
    calls=[];uploads=[]
    async def api(config,endpoint,payload):
        calls.append((endpoint,payload))
        return {'upload_param':'upload token'} if endpoint.endswith('getuploadurl') else {'ret':0}
    class Response:
        status=200;headers={'x-encrypted-param':'download token'}
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
    class Client:
        def __init__(self,**kwargs):self.connector=kwargs['connector']
        async def __aenter__(self):return self
        async def __aexit__(self,*args):await self.connector.close()
        def post(self,url,**kwargs):uploads.append((url,kwargs));return Response()
    monkeypatch.setattr(wire,'api',api)
    monkeypatch.setattr(wire.aiohttp,'ClientSession',Client)
    async def run():
        config=SimpleNamespace(cdn_base_url='https://novac2c.cdn.weixin.qq.com/c2c')
        item=await wire.upload_image(config,'recipient',PNG)
        body=calls[0][1];key=bytes.fromhex(body['aeskey'])
        assert body['rawfilemd5']==hashlib.md5(PNG).hexdigest() and body['media_type']==1 and body['no_need_thumb']
        assert uploads[0][1]['headers']=={'Content-Type':'application/octet-stream'}
        decryptor=Cipher(algorithms.AES(key),modes.ECB()).decryptor()
        padded=decryptor.update(uploads[0][1]['data'])+decryptor.finalize()
        unpadder=padding.PKCS7(128).unpadder()
        assert unpadder.update(padded)+unpadder.finalize()==PNG
        assert item['image_item']['mid_size']==len(uploads[0][1]['data'])
        assert base64.b64decode(item['image_item']['media']['aes_key']).decode()==key.hex()
        await wire.send_item(config,'recipient','context',item,'stable-id')
        assert calls[-1][1]['msg']['item_list']==[item]
        assert calls[-1][1]['msg']['context_token']=='context'
    asyncio.run(run())

def test_dns_blocks_private_and_limits_fake_ip(monkeypatch):
    async def resolve(self,host,port=0,family=0):return [{'host':'198.18.0.35'}]
    monkeypatch.setattr(wire.aiohttp.resolver.ThreadedResolver,'resolve',resolve)
    async def run():
        resolver=wire.PublicResolver()
        assert await resolver.resolve('sns-webpic-qc.xhscdn.com')
        with pytest.raises(ValueError):await resolver.resolve('untrusted.example')
        await resolver.close()
    asyncio.run(run())

def test_empty_api_acknowledgment(monkeypatch):
    class Response:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def read(self,*args):return b''
    monkeypatch.setattr(wire.urllib_request,'urlopen',lambda *a,**k:Response())
    assert wire.post_api('https://ilinkai.weixin.qq.com/ilink/bot/sendmessage',{}, {})=={}

def test_explicit_api_rejection(monkeypatch):
    monkeypatch.setattr(wire,'post_api',lambda *a:{'ret':-14,'errcode':-14})
    with pytest.raises(wire.ImageAPIRejected,match='ret=-14'):
        asyncio.run(wire.api(SimpleNamespace(base_url='https://example.com',token='test'),'sendmessage',{}))

@pytest.mark.parametrize('kind',['stale','transport','other','prepare'])
def test_context_recovery_only_after_explicit_rejection(monkeypatch,kind):
    calls=[]
    async def api(config,endpoint,payload):
        calls.append(json.loads(json.dumps(payload)))
        if len(calls)==1:
            if kind=='transport':raise TimeoutError()
            if kind=='other':raise wire.ImageAPIRejected(-5,None,'denied')
            if kind=='prepare':raise wire.ImageAPIRejected(-2,None,'prepare failed')
            raise wire.ImageAPIRejected(-2,None,'unknown error')
        return {'ret':0}
    monkeypatch.setattr(wire,'api',api)
    async def run():
        if kind=='stale':
            result=await wire.send_item(None,'recipient','stale',{'type':2},'stable-id')
            assert result['context_reset'] and len(calls)==2
            assert 'context_token' not in calls[1]['msg']
            assert calls[0]['msg']['client_id']==calls[1]['msg']['client_id']
        else:
            with pytest.raises((TimeoutError,wire.ImageAPIRejected)):
                await wire.send_item(None,'recipient','stale',{'type':2},'stable-id')
            assert len(calls)==1
    asyncio.run(run())


def test_prepare_failure_requires_new_message_and_preserves_receipt(tmp_path,monkeypatch):
    sent=[]
    async def download(url):return PNG
    async def upload(*args):return {'type':2,'image_item':{}}
    async def send(*args):
        sent.append(args)
        raise wire.ImageAPIRejected(-2,None,'prepare failed')
    monkeypatch.setattr(delivery,'download_image',download)
    monkeypatch.setattr(delivery,'upload_image',upload)
    monkeypatch.setattr(delivery,'send_item',send)
    async def run():
        data={'session':'current-session','id':'rejected-cover','args':{'image_url':'https://example.com/image.jpg','title':'标题'}}
        result=await delivery.share_image(fake_channel(tmp_path),data)
        assert result['status']=='failed' and result['requires_new_message']
        assert result['image']=='rejected' and result['caption']=='not_sent'
        assert await delivery.share_image(fake_channel(tmp_path),data)==result
        assert len(sent)==1
    asyncio.run(run())
