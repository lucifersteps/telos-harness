import asyncio,json
from urllib.parse import parse_qs,urlparse
from support import Harness,call
from telos.tools.baidu_maps import BaiduMaps
from business_tests.test_maps import Opener


def test_maps_followup_reuses_verified_place_after_native_restart(tmp_path,monkeypatch):
    monkeypatch.setenv('BAIDU_MAP_AUTH_TOKEN','isolated-map-token')
    async def run():
        place={'operation':'place','parameters':{'user_raw_request':'甲商圈','region':'虚构市'}}
        route={'operation':'direction','parameters':{
            'user_raw_request':'从甲商圈到乙商圈乘地铁','location':'39.900001,116.400001'}}
        h=Harness(tmp_path,[call('read',{'name':'baidu-ai-map'},name='skill'),
            call('place',place,name='baidu_maps'),'查到甲商圈。',call('route',route,name='baidu_maps'),'乘地铁约30分钟。'])
        opener=Opener({'status':0,'results':[{'name':'甲商圈','uid':'poi-1','location':{'lat':39.900001,'lng':116.400001}}]})
        try:
            h.runtime.business.maps=BaiduMaps(opener=opener)
            await h.submit('找到虚构市的甲商圈')
            await h.runtime.close();h.runtime=h.restart()
            opener.response={'status':0,'result':{'answer_type':'gptmodel_navigate',
                'navigation_data':{'public_routes':[{'duration':1800,'steps':[],'price':5}]}}}
            h.runtime.business.maps=BaiduMaps(opener=opener)
            await h.submit('从那里到乙商圈地铁多久？')
            assert len(opener.requests)==2 and not h.native.writes
            assert '30分钟' in h.sent[-1]
            args=parse_qs(urlparse(opener.requests[-1][0].full_url).query)
            assert args['location']==['39.900001,116.400001']
            assert 'isolated-map-token' not in json.dumps(h.model.requests,ensure_ascii=False)
        finally:await h.close()
    asyncio.run(run())


def test_read_failure_returns_to_loop_without_being_misreported_as_argument_error(tmp_path):
    async def run():
        h=Harness(tmp_path,[call('read',{'action':'query'}),'目前无法核实提醒事项，请稍后再试。'])
        try:
            h.native.fail=True
            await h.submit('查询提醒事项')
            assert '无法核实' in h.sent[-1] and not h.native.writes
            history=json.dumps(h.model.requests[-1],ensure_ascii=False)
            assert 'tool_failed' in history and 'input_error' not in history
        finally:await h.close()
    asyncio.run(run())


def test_map_followup_accepts_user_coordinates_from_existing_inbox_after_restart(tmp_path,monkeypatch):
    monkeypatch.setenv('BAIDU_MAP_AUTH_TOKEN','isolated-map-token')
    async def run():
        query={'operation':'direction','parameters':{'user_raw_request':'从用户指定起点到乙餐厅驾车',
            'location':'39.900001,116.400001'}}
        h=Harness(tmp_path,['收到起点。',call('route',query,name='baidu_maps'),'行驶约30分钟。'])
        opener=Opener()
        try:
            await h.submit('这是本次行程起点的坐标：39.900001,116.400001')
            await h.runtime.close();h.runtime=h.restart();h.runtime.business.maps=BaiduMaps(opener=opener)
            await h.submit('从那里到乙餐厅多久？')
            assert len(opener.requests)==1 and not h.native.writes
            result=[e['data']['message'] for e in h.events if e['type']=='tool/result'][-1]
            assert result.get('isError') is False
        finally:await h.close()
    asyncio.run(run())
