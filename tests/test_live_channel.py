"""新增通道/Skill 真实模型评测；与原18项分开统计，外部写入和地图响应均模拟。"""
import asyncio
import io
import json
import os
import time
from pathlib import Path
from urllib.parse import parse_qs,urlparse
import pytest
from support import Harness
from test_live import connection
from telos.tools.baidu_maps import BaiduMaps

pytestmark=pytest.mark.skipif(not os.getenv('TELOS_NATIVE_MODEL_HOME'),reason='须显式启用真实模型评测')

class Maps:
    def __init__(self,failed=False):self.requests=[];self.failed=failed
    def open(self,request,timeout):
        parsed=urlparse(request.full_url);params=parse_qs(parsed.query);self.requests.append((parsed.path,params))
        if parsed.path.endswith(('/place','/geocoding')):
            result={'status':0,'results':[{'name':'甲站地铁站','uid':'test-origin','location':{'lat':39.900001,'lng':116.400001}},
                                        {'name':'乙餐厅','uid':'test-destination','location':{'lat':39.910001,'lng':116.410001}}],
                    'result':{'level':'兴趣点','location':{'lat':39.900001,'lng':116.400001}}}
        else:
            text=params.get('user_raw_request',[''])[0]
            driving='驾车' in text
            if self.failed and driving:result={'status':1,'message':'模拟服务暂时无法返回驾车路线'}
            else:result={'status':0,'result':{'answer_type':'gptmodel_navigate','navigation_data':{
                'driving_routes' if driving else 'public_routes':[{'duration':2700 if driving else 1800,'distance':12000,
                    'steps':[{'instructions':'驾车沿测试路前往乙餐厅' if driving else '步行进入甲站，乘测试地铁1号线到乙站后步行至餐厅'}]}]}}}
        return io.BytesIO(json.dumps(result,ensure_ascii=False).encode())

def save(row,h,start):
    row.update(seconds=time.perf_counter()-start,usage=[e['data'].get('usage',{}) for e in h.events if e['type']=='assistant/message'],
        reply='\n'.join(h.sent),writes=len(h.native.writes))
    with Path(os.environ['TELOS_NATIVE_REPORT']).open('a') as f:f.write(json.dumps(row,ensure_ascii=False)+'\n')

@pytest.mark.parametrize('failed',[False,True],ids=['map-two-modes','map-partial-failure'])
def test_live_native_skill_map_and_apple(tmp_path,monkeypatch,failed):
    monkeypatch.setenv('BAIDU_MAP_AUTH_TOKEN','isolated-test-only')
    async def run():
        h=Harness(tmp_path,[],connection=connection());opener=Maps(failed);h.runtime.business.maps=BaiduMaps(opener=opener)
        row={'case':'map-partial-failure' if failed else 'map-two-modes','passed':False};start=time.perf_counter()
        try:
            await h.submit('以下全是虚构评测数据。创建“虚构路线测试聚餐”，2027年2月19日18点到20点，在虚构市乙餐厅。'
                '另外，通过地图工具查询当前从虚构市甲站地铁站到乙餐厅的地铁和打车耗时，分别告诉我结果。')
            text='\n'.join(h.sent)
            calls=[e['data'].get('name') for e in h.events if e['type']=='tool/call']
            # Native tool/call envelopes may nest the call; inspect the canonical recorded JSON too.
            trace=json.dumps([e for e in h.events if e['type']=='tool/call'])
            assert '"skill"' in trace and '"baidu_maps"' in trace
            assert h.runtime.business.pending(h.session)['kind']=='approval' and not h.native.writes
            assert '30' in text or '半小时' in text
            if failed:assert any(w in text for w in ['无法','失败','未能','暂时','未返回'])
            else:assert '45' in text
            assert 'isolated-test-only' not in json.dumps(h.events)
            row['passed']=True;row['map_requests']=opener.requests
        finally:save(row,h,start);await h.close()
    asyncio.run(run())


def test_live_steer_revision_then_confirm(tmp_path):
    async def run():
        h=Harness(tmp_path,[],connection=connection());entered=asyncio.Event();release=asyncio.Event()
        execute=h.runtime.business.execute
        async def hold(*args):
            result=await execute(*args)
            if result.get('status')=='awaiting_approval' and not entered.is_set():
                entered.set();await asyncio.wait_for(release.wait(),20)
            return result
        h.runtime.business.execute=hold
        row={'case':'steer-revision-confirm','passed':False};start=time.perf_counter()
        try:
            first=asyncio.create_task(h.submit('以下全部是虚构评测。创建“虚构连续补充会议”，2027年2月19日14点到15点，备注带材料，提前30分钟提醒。',key='initial'))
            await asyncio.wait_for(entered.wait(),120)
            second=asyncio.create_task(h.submit('刚才时间改为16点到17点，其他要求不变。',key='revision'))
            await asyncio.sleep(.1);release.set();await asyncio.gather(first,second)
            pending=h.runtime.business.pending(h.session)
            assert pending and pending['kind']=='approval' and not h.native.writes and len(h.sent)==1
            preview=pending['preview'];fields=(preview.get('operations') or [preview])[0]['fields']
            assert '16:00' in fields['start'] and '17:00' in fields['end']
            assert fields['notes']=='带材料' and fields['alarm_minutes_before']==[30]
            await h.submit('1',key='confirmation')
            assert len(h.native.writes)==1 and '已创建成功' in h.sent[-1]
            assert h.native.writes[0][1]['fields']['notes']=='带材料'
            assert h.native.writes[0][1]['fields']['alarm_minutes_before']==[30]
            await h.submit('1',key='confirmation')
            assert len(h.native.writes)==1
            row['passed']=True
        finally:release.set();save(row,h,start);await h.close()
    asyncio.run(run())
