"""Skill 与个人任务循环联测；不访问真实地图、模型或 Apple。"""
import asyncio
import io
import json
from dataclasses import replace
from urllib.error import HTTPError, URLError

import pytest

from telos.tools.baidu_maps import BaiduMaps, _NoRedirect, validate_query, query_result


def skill(action='skill_call', **kw):
    return dict(action=action, skill='baidu-ai-map', **kw)


def query(**kw):
    return skill(operation='direction', parameters={'user_raw_request': '从甲站到乙站乘地铁，今晚18点到达',
                 'location': '39.900001,116.400001'}, **kw)


class Opener:
    def __init__(self, response=None, error=None):
        self.response = response or {'status': 0, 'result': {'answer_type': 'gptmodel_navigate',
            'navigation_data': {'public_routes': [{'duration': 1800, 'steps': [], 'price': 5}]}}}
        self.error = error
        self.requests = []

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        if self.error:
            raise self.error
        return io.BytesIO(json.dumps(self.response).encode())


def test_token_is_backend_only_and_request_preserves_constraints(monkeypatch):
    monkeypatch.setenv('BAIDU_MAP_AUTH_TOKEN', 'secret-test-token')
    opener = Opener()
    opener.response['result']['text'] = 'secret-test-token'
    args = query()
    result = asyncio.run(BaiduMaps(opener=opener).query(args['operation'], args['parameters']))
    request, timeout = opener.requests[0]
    from urllib.parse import parse_qs, urlparse
    params = parse_qs(urlparse(request.full_url).query)
    assert params['user_raw_request'] == [args['parameters']['user_raw_request']]
    assert request.get_header('Authorization') == 'Bearer secret-test-token'
    assert 'secret-test-token' not in request.full_url
    assert 'secret-test-token' not in json.dumps(result)
    assert result['status'] == 'skill_result' and timeout == 40
    assert _NoRedirect().redirect_request(None, None, 302, '', {}, 'https://example.org') is None


def test_missing_token_does_not_request(monkeypatch):
    monkeypatch.delenv('BAIDU_MAP_AUTH_TOKEN', raising=False)
    opener = Opener()
    result = asyncio.run(BaiduMaps(opener=opener).query('weather', {'region': '北京市'}))
    assert result['status'] == 'unavailable' and not opener.requests


def test_skill_rejects_guessed_coordinates_and_accepts_saved_tool_evidence(tmp_path):
    maps=BaiduMaps()
    with pytest.raises(ValueError,match='坐标没有'):
        maps.resolve({k:v for k,v in query().items() if k!="action"},state={})
    state={'task_inputs':[{'text':'从这里出发：39.900001,116.400001'}]}
    assert maps.resolve({k:v for k,v in query().items() if k!="action"},state=state) is not None
    output={'status':'skill_result','source':'百度地图 Agent Plan','operation':'place','response':{'results':[{'name':'甲商圈','uid':'poi-1','location':{'lat':39.900001,'lng':116.400001}}]}}
    state={'checklist':[{'evidence':{'call':{'action':'skill_call','output':output}}}]}
    with pytest.raises(ValueError,match='坐标没有'):
        maps.resolve({k:v for k,v in query().items() if k!='action'},state=state)
    from types import SimpleNamespace
    records=[SimpleNamespace(tool_name='baidu_maps',args={'skill':'baidu-ai-map'},result=output,is_error=False)]
    mapped=query()
    mapped['parameters']['refer_pois']='甲商圈:poi-1,39.900001,116.400001'
    assert maps.resolve({k:v for k,v in mapped.items() if k!="action"},state=state,records=records) is not None
    mapped['parameters']['refer_pois']='甲地铁站:poi-1,39.900001,116.400001'
    with pytest.raises(ValueError,match='共同匹配'):
        maps.resolve({k:v for k,v in mapped.items() if k!="action"},state=state,records=records)
    output.update(status='needs_clarification',operation='geocoding')
    with pytest.raises(ValueError,match='坐标没有'):
        maps.resolve({k:v for k,v in query().items() if k!="action"},state=state)


@pytest.mark.parametrize('operation,params', [
    ('https://evil.test', {}), ('direction', {'user_raw_request':'去乙站'}),
    ('direction', {'user_raw_request':'去乙站', 'location':'NaN,116'}),
    ('place', {'user_raw_request':'最近咖啡店', 'region':'北京市', 'sort':'distance'}),
    ('weather', {}), ('weather', {'region':'北京市', 'token':'secret'}),
    ('direction', {'user_raw_request':'去乙站', 'location':'39,116', 'refer_pois':'bad'}),
])
def test_invalid_params_rejected(operation, params):
    with pytest.raises(ValueError):
        validate_query(operation, params)


def test_candidates_and_county_centers_are_not_query_success():
    for answer in ['gptmodel_poi_clarify','gptmodel_onway_search_clarify']:
        result=query_result('direction',{}, {'status':0, 'result':{'answer_type':answer,'poi_clarify_data':{'data':[{'name':'甲站'}]}}})
        assert result['status']=='needs_clarification' and result['response']['result']['poi_clarify_data']
    assert query_result('geocoding',{}, {'status':0,'result':{'level':'区县','location':{'lat':39,'lng':116}}})['status']=='needs_clarification'
    assert query_result('direction',{}, {'status':0,'result':{'answer_type':'gptmodel_navigate'}})['status']=='unavailable'


def test_geometry_removed_but_route_details_retained():
    response=Opener().response
    route=response['result']['navigation_data']['public_routes'][0]
    route['steps']=[{'path':'long polyline', 'instructions':'换乘2号线', 'duration':600, 'distance':400}]
    result=query_result('direction',{},response)
    saved=result['response']['result']['navigation_data']['public_routes'][0]
    assert saved['duration']==1800 and saved['price']==5
    assert saved['steps']==[{'instructions':'换乘2号线', 'duration':600, 'distance':400}]
    assert 'path' in route['steps'][0]


def test_future_driving_requires_real_timing_and_keeps_forecast():
    data={'status':0,'result':{'answer_type':'gptmodel_future','future_data':{
        'departure_time':1792400400,'arrival_time':1792402550,
        'driving_routes':[{'duration':2150,'distance':15302}], 'future_session':[{'selected':True}]}}}
    result=query_result('direction',{},data)
    assert result['status']=='skill_result'
    assert result['forecast_times']=={'departure_time':'2026-10-19T17:00:00+08:00',
                                     'arrival_time':'2026-10-19T17:35:50+08:00'}
    assert result['response']['result']['future_data']['future_session']==[{'selected':True}]
    del data['result']['future_data']['departure_time']
    assert query_result('direction',{},data)['status']=='unavailable'


@pytest.mark.parametrize('response,error', [
    ({'status': 4, 'message': 'quota'}, None),
    ({'error': 'invalid token'}, None),
    (None, URLError('network secret-test-token')),
    (None, HTTPError('https://api.map.baidu.com', 401, 'token error', {}, None)),
])
def test_service_errors_are_not_success(monkeypatch, response, error):
    monkeypatch.setenv('BAIDU_MAP_AUTH_TOKEN', 'secret-test-token')
    result = asyncio.run(BaiduMaps(opener=Opener(response,error)).query('weather', {'region':'北京市'}))
    assert result['status'] == 'unavailable'
    assert 'secret-test-token' not in json.dumps(result)
