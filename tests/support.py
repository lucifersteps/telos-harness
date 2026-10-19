"""测试专用 OpenAI 协议模型和 Apple；生产路径使用 Harness 官方模型适配器。"""
import asyncio
from copy import deepcopy
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
from pathlib import Path
import threading
from types import SimpleNamespace

from telos.adapters.base import ConversationRef,SendResult
from telos.adapters.wechat.adapter import WeChatAdapter
from telos.connectors.apple.native import AppleNativeError
from telos.tools.apple.service import AppleService
from runtime import Runtime,ROOT


def call(cid,args,name='personalPlan'):
    return {'tool_calls':[{'id':cid,'type':'function','function':{'name':name,'arguments':json.dumps(args,ensure_ascii=False)}}]}

def draft(title='测试会议'):
    return {'action':'create','kind':'event','fields':{'title':title,'calendar_id':'calendar-event',
        'start':'2027-02-19T14:00:00+08:00','end':'2027-02-19T15:00:00+08:00'}}

class Model:
    def __init__(self,script):
        self.script=list(script);self.requests=[];owner=self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                request=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                owner.requests.append(request)
                owner.last_path=self.path
                item=owner.script.pop(0) if owner.script else '处理完毕。'
                if callable(item):item=item(request)
                message={'role':'assistant','content':item} if isinstance(item,str) else {'role':'assistant','content':'',**item}
                reason='tool_calls' if message.get('tool_calls') else 'stop'
                common={'id':f'test-{len(owner.requests)}','model':request['model'],'created':1}
                self.send_response(200)
                if self.path.endswith('/messages') and request.get('stream'):
                    self.send_header('Content-Type','text/event-stream');self.end_headers()
                    chunks=[{'type':'message_start','message':{'id':common['id'],'type':'message','role':'assistant',
                        'model':request['model'],'content':[],'stop_reason':None,'stop_sequence':None,
                        'usage':{'input_tokens':10,'output_tokens':0}}}]
                    blocks=[]
                    if message.get('content'):blocks.append({'type':'text','text':message['content']})
                    for c in message.get('tool_calls',[]):blocks.append({'type':'tool_use','id':c['id'],
                        'name':c['function']['name'],'arguments':c['function']['arguments']})
                    for index,b in enumerate(blocks):
                        start={'type':'text','text':''} if b['type']=='text' else {'type':'tool_use','id':b['id'],'name':b['name'],'input':{}}
                        delta={'type':'text_delta','text':b['text']} if b['type']=='text' else {'type':'input_json_delta','partial_json':b['arguments']}
                        chunks.extend([{'type':'content_block_start','index':index,'content_block':start},
                            {'type':'content_block_delta','index':index,'delta':delta},{'type':'content_block_stop','index':index}])
                    chunks.extend([{'type':'message_delta','delta':{'stop_reason':'tool_use' if reason=='tool_calls' else 'end_turn','stop_sequence':None},
                                    'usage':{'output_tokens':5}}, {'type':'message_stop'}])
                    body=''.join('event: '+x['type']+'\ndata: '+json.dumps(x)+'\n\n' for x in chunks)
                elif request.get('stream'):
                    self.send_header('Content-Type','text/event-stream');self.end_headers()
                    delta={**message}
                    if 'tool_calls' in delta:delta['tool_calls']=[{'index':i,**c} for i,c in enumerate(delta['tool_calls'])]
                    chunks=[{**common,'object':'chat.completion.chunk','choices':[{'index':0,'delta':delta,'finish_reason':None}]},
                        {**common,'object':'chat.completion.chunk','choices':[{'index':0,'delta':{},'finish_reason':reason}],
                         'usage':{'prompt_tokens':10,'completion_tokens':5,'total_tokens':15}}]
                    body=''.join('data: '+json.dumps(x)+'\n\n' for x in chunks)+'data: [DONE]\n\n'
                else:
                    self.send_header('Content-Type','application/json');self.end_headers()
                    body=json.dumps({**common,'object':'chat.completion','choices':[{'index':0,'message':message,'finish_reason':reason}],
                                     'usage':{'prompt_tokens':10,'completion_tokens':5,'total_tokens':15}})
                try:self.wfile.write(body.encode())
                except BrokenPipeError:pass
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        threading.Thread(target=self.server.serve_forever,daemon=True).start()
        self.url=f'http://127.0.0.1:{self.server.server_port}/v1'
    def close(self):self.server.shutdown();self.server.server_close()

class Apple:
    def __init__(self):
        self.items=[{'id':'reminder-001','kind':'reminder','calendar_id':'list-001','title':'买菜','notes':'','completed':False,'due':'2026-10-10'}]
        self.calendars=[{'id':'list-001','title':'生活','kind':'reminder','writable':True},
                        {'id':'calendar-event','title':'Telos-日程','kind':'event','writable':True}]
        self.writes=[];self.fail=False;self.fail_at=None;self.on_write=None
    def request(self,action,args):
        if self.fail:raise AppleNativeError('暂时无法读取',code='read_failed')
        if action=='snapshot':return {'items':deepcopy(self.items),'calendars':deepcopy(self.calendars)}
        if action=='detail':
            item=next((x for x in self.items if x['id']==args['id']),None)
            if not item:raise AppleNativeError('已删除',code='not_found')
            return {'item':deepcopy(item)}
        self.writes.append((action,deepcopy(args)))
        if len(self.writes)==self.fail_at:raise AppleNativeError('模拟写入结果未知',code='transport_error')
        if action=='create':
            item={'id':f'new-{len(self.writes)}','kind':args['kind'],**args['fields']};self.items.append(item)
        else:
            item=next(x for x in self.items if x['id']==args['id'])
            assert item==args['expected']
            if action=='delete':self.items.remove(item);return {'saved':True}
            item.update(args.get('fields',{}))
        if self.on_write:self.on_write()
        return {'saved':True,'item':deepcopy(item)}

class Harness:
    def __init__(self,path,script,*,connection=None):
        self.path=path;self.model=Model(script) if connection is None else None
        self.connection=connection or {'base_url':self.model.url,'api_key':'isolated-test-key','model':'deepseek-flash'}
        self.native=Apple();self.apple=AppleService(self.native,path/'apple');self.sent=[];self.index=0;self.events=[]
        self.ref=ConversationRef(platform='wechat',chat_id='test',user_id='test',source_id='dm:test')
        async def send(ref,text):self.sent.append(text);return SendResult(ok=True)
        self.adapter=WeChatAdapter(send_text_fn=send)
        self.runtime=self.restart();self.session='wechat-test'
    def record_events(self,events):
        seen={e['seq'] for e in self.events}
        self.events.extend(e for e in events if e['seq'] not in seen)
    def restart(self):return Runtime(self.path/'data',self.apple,self.adapter,event_sink=self.record_events,**self.connection)
    async def submit(self,text,key=None,**kwargs):
        self.index+=1;await self.runtime.handle(self.session,key or str(self.index),text,self.ref,**kwargs)
    async def close(self):
        await self.runtime.close()
        if self.model:await asyncio.to_thread(self.model.close)

