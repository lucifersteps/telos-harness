"""百度 Agent Plan 只读接口；凭据仅由宿主注入，不经过模型参数。"""
from __future__ import annotations

import asyncio
import json
import math
import os
import re
from datetime import datetime
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from telos.agent.tools.types import ToolExecution
from telos.core.clock import USER_TIMEZONE, user_now_iso

ENDPOINTS = {
    'place': ({'user_raw_request', 'region'}, {'center', 'sort'}),
    'direction': ({'user_raw_request', 'location'}, {'refer_pois'}),
    'geocoding': ({'address'}, {'region'}),
    'reverse_geocoding': ({'location'}, set()),
    'weather': (set(), {'region', 'location'}),
}
PARAMETERS = {
    'type': 'object', 'additionalProperties': False,
    'properties': {key: {'type': 'string', 'minLength': 1, 'maxLength': 8000}
                   for key in sorted(set().union(*(a | b for a, b in ENDPOINTS.values())))},
}


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Authorization 不能被重定向到其他域名。


def _without_geometry(value):
    """按 JSON 字段去掉线路折线，保留方案、路况、步骤、换乘、耗时及费用。"""
    if isinstance(value, dict):
        return {key: _without_geometry(item) for key, item in value.items() if key != 'path'}
    if isinstance(value, list):
        return [_without_geometry(item) for item in value]
    return value


def query_result(operation, parameters, data):
    result = {'status': 'skill_result', 'source': '百度地图 Agent Plan', 'checked_at': user_now_iso(),
              'operation': operation, 'parameters': parameters, 'response': _without_geometry(data)}
    if operation == 'direction':
        body = data.get('result') or {}
        answer = body.get('answer_type') if isinstance(body, dict) else None
        navigation = body.get('future_data' if answer == 'gptmodel_future' else 'navigation_data') if isinstance(body, dict) else None
        if answer in {'gptmodel_poi_clarify', 'gptmodel_onway_search_clarify'}:
            result.update(status='needs_clarification', message='返回的是地点候选，还没有路线。核对名称、类别、地址；无法唯一对应用户要求时询问用户，再携带所选名称和 refer_pois 继续。')
        elif (answer not in {'gptmodel_navigate', 'gptmodel_future'} or not isinstance(navigation, dict)
              or not any(navigation.get(key) for key in ('public_routes', 'driving_routes', 'walking_routes', 'riding_routes'))):
            result.update(status='unavailable', message='没有识别到完整路线方案，不能声称路线查询完成。')
        elif answer == 'gptmodel_future':
            try:
                result['forecast_times'] = {key: datetime.fromtimestamp(navigation[key], USER_TIMEZONE).isoformat()
                                            for key in ('departure_time', 'arrival_time')}
            except (KeyError, TypeError, ValueError, OSError, OverflowError):
                result.update(status='unavailable', message='未来路线缺少有效出发/到达时间，不能确认预测时段。')
    elif operation == 'geocoding':
        body = data.get('result') or {}
        if (not isinstance(body, dict) or not body.get('location')
                or body.get('level') in {'国家', '省', '城市', '区县'}):
            result.update(status='needs_clarification', message='未取得精确地点坐标，不能把行政区域中心作为具体起终点；请检索更具体地点。')
    elif operation == 'place' and not data.get('results'):
        result.update(status='unavailable', message='未返回可用地点，请核对查询与区域。')
    return result


def _coordinate(value):
    try:
        lat, lng = map(float, value.split(','))
        if not (math.isfinite(lat) and math.isfinite(lng) and -90 <= lat <= 90 and -180 <= lng <= 180):
            raise ValueError
    except (ValueError, AttributeError):
        raise ValueError('坐标须为 GCJ-02 的纬度,经度，且来自用户或可信查询结果。') from None
    return f'{lat:.6f},{lng:.6f}'


def validate_query(operation, parameters):
    if operation not in ENDPOINTS:
        raise ValueError('仅支持 place、direction、geocoding、reverse_geocoding、weather。')
    required, optional = ENDPOINTS[operation]
    if not isinstance(parameters, dict) or set(parameters) - required - optional or required - set(parameters):
        raise ValueError(f'{operation} 参数不匹配；必填：{sorted(required)}；可选：{sorted(optional)}。')
    if any(not isinstance(v, str) or not v.strip() or len(v) > 8000 for v in parameters.values()):
        raise ValueError('查询参数必须是非空文字，且不超过 8000 字。')
    result = dict(parameters)
    for name in ('location', 'center'):
        if name in result:
            result[name] = _coordinate(result[name])
    if operation == 'weather' and not result:
        raise ValueError('天气查询必须提供 region 或 location。')
    if 'sort' in result and result['sort'] not in {'distance', 'relevance'}:
        raise ValueError('sort 只能为 distance 或 relevance。')
    if result.get('sort') == 'distance' and 'center' not in result:
        raise ValueError('按距离排序必须提供可靠的 center。')
    if 'refer_pois' in result:
        for item in result['refer_pois'].split(';'):
            try:
                name, poi = item.rsplit(':', 1)
                uid, lat, lng = poi.split(',')
                if not name or not uid:
                    raise ValueError
                _coordinate(f'{lat},{lng}')
            except ValueError:
                raise ValueError('refer_pois 格式为 地点名称:uid,纬度,经度，多项用分号分隔。') from None
    return result


def trusted_locations(state, records=()):
    """从当前用户文字和同会话地图记录取地点事实；不继承任务、路线耗时或预测。"""
    coordinates = set()
    pois = set()
    def collect(value):
        if isinstance(value, dict):
            if 'lat' in value and 'lng' in value:
                try:
                    coordinates.add(_coordinate(f"{value['lat']},{value['lng']}"))
                except ValueError:
                    pass
            info = value.get('info') if isinstance(value.get('info'), dict) else value
            location = value.get('location')
            if info.get('name') and info.get('uid') and isinstance(location, dict):
                try:
                    pois.add((info['name'], info['uid'], _coordinate(f"{location['lat']},{location['lng']}")))
                except (ValueError, KeyError, TypeError):
                    pass
            for item in value.values():
                collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)
    for item in state.get('task_inputs', []):
        for lat, lng in re.findall(r'(?<![\d.])([-+]?\d{1,3}\.\d+)\s*[,，]\s*([-+]?\d{1,3}\.\d+)(?![\d.])', item.get('text', '')):
            collect({'lat':lat, 'lng':lng})
    for record in records:
        output = record.result
        if (record.tool_name == 'baidu_maps'
                and isinstance(output, dict) and not record.is_error
                and output.get('operation') in ENDPOINTS
                and output.get('status') in {'skill_result', 'needs_clarification'}
                and not (output.get('operation') == 'geocoding' and output.get('status') != 'skill_result')):
            collect(output.get('response', {}))
    return coordinates, pois


class BaiduMaps:
    def __init__(self, *, opener=None):
        self.opener = opener or build_opener(_NoRedirect())

    def resolve(self, args, *, state=None, records=(), token=None):
        operation = args.get('operation')
        parameters = validate_query(operation, args.get('parameters', {}))
        provided = [parameters[key] for key in ('location', 'center') if key in parameters]
        trusted, pois = trusted_locations(state or {}, records)
        for poi in parameters.get('refer_pois', '').split(';'):
            if poi:
                name, reference = poi.rsplit(':', 1)
                uid, coordinate = reference.split(',', 1)
                if (name, uid, _coordinate(coordinate)) not in pois:
                    raise ValueError('refer_pois 的名称、uid、坐标必须共同匹配已返回的同一地点，不能把商圈/酒店改名为车站。可先不传该映射，以明确起终点查询并获取真正候选，再用原样名称消歧。')
                provided.append(coordinate)
        if any(_coordinate(value) not in trusted for value in provided):
            raise ValueError('坐标没有用户输入或已保存工具结果作为依据。先调用 place/geocoding 取得可信坐标；不得使用记忆、估计或取整坐标。保留原始起终点和时间要求。')
        return ToolExecution(execute=lambda: self.query(operation, parameters, token=token))

    async def query(self, operation, parameters, *, token=None):
        parameters = validate_query(operation, parameters)
        token = (os.environ.get('BAIDU_MAP_AUTH_TOKEN', '') if token is None else token).strip()
        if not token:
            return {'status': 'unavailable', 'message': '尚未配置百度 Agent Plan Token；请在 DSH 凭据或启动环境中配置 BAIDU_MAP_AUTH_TOKEN。'}
        return await asyncio.to_thread(self._query, operation, parameters, token)

    def _query(self, operation, parameters, token):
        url = f'https://api.map.baidu.com/agent_plan/v1/{operation}?' + urlencode(parameters)
        request = Request(url, headers={'Authorization': f'Bearer {token}', 'Accept': 'application/json'})
        try:
            with self.opener.open(request, timeout=40) as response:
                raw = response.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                return {'status': 'unavailable', 'message': '百度返回超过 1 MiB，未截断为路线结果，请缩小查询。'}
            # 完整解析 JSON；即使服务回显凭据，也不得进入会话与模型上下文。
            data = json.loads(raw.decode('utf-8').replace(token, '[REDACTED]'))
            if not isinstance(data, dict) or not data:
                raise ValueError
        except HTTPError as error:
            return {'status': 'unavailable', 'message': f'百度接口 HTTP {error.code}；请核对 Token、额度或服务状态。'}
        except (URLError, OSError, ValueError):
            return {'status': 'unavailable', 'message': '百度查询超时、网络失败或响应不是有效 JSON；未取得可用结果。'}
        # 不把 HTTP 200 的业务错误当成功。未知业务结构仍交给模型核对，不能凭 HTTP 状态声称有路线。
        if data.get('error') or any(str(data[k]).lower() not in {'0', '200', 'ok', 'success'}
                                    for k in ('status', 'code', 'errno') if k in data):
            return {'status': 'unavailable', 'message': '百度接口返回业务错误。', 'response': data}
        return query_result(operation, parameters, data)
