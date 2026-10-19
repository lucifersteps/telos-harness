"""地点只使用 Apple 地图结果，正式预览前核实字段，原生写入边界保留。"""
import asyncio
from copy import deepcopy

import pytest

from telos.connectors.apple.native import AppleNativeError
from telos.tools.apple.locations import validate_location
from telos.tools.apple.service import AppleService
from business_tests.test_interaction import make_tool, resolve
from business_tests.test_apple_service import FakeApple

PLACE = {'title': '虚构公寓', 'address': '虚构市测试街45号', 'latitude': 0.0, 'longitude': 0.0}


class LocationApple(FakeApple):
    def __init__(self):
        super().__init__()
        self.searches = []
        self.places = [deepcopy(PLACE)]

    def request(self, action, args):
        if action == 'search_locations':
            self.searches.append(args['query'])
            return {'locations': deepcopy(self.places)}
        if action in ('create', 'update') and args['kind'] == 'reminder' and args['fields'].get('location') is not None:
            validate_location(args['fields']['location'])
        return super().request(action, args)


def reminder(location):
    return {'action': 'create', 'kind': 'reminder',
            'fields': {'title': '虚构地点测试', 'calendar_id': 'list-001', 'location': location}}


def test_text_location_only_searches_and_returns_persistent_candidates(tmp_path):
    tool, native, apple = make_tool(tmp_path, native=LocationApple())
    execution = resolve(tool, reminder('虚构市测试街45号'))
    assert not execution.need_confirm
    result = asyncio.run(execution.run())
    assert result['status'] == 'location_candidates' and not native.writes
    place = result['candidates'][0]
    restarted = AppleService(native, tmp_path / 'cache')
    resolved = restarted.resolve_location({'place_id': place['place_id'], 'trigger': 'arrive'})
    assert resolved == {**PLACE, 'trigger': 'arrive', 'radius': 200}


@pytest.mark.parametrize('trigger', ['arrive', 'leave'])
def test_location_preview_saves_exact_selected_coordinates_and_preserves_time(tmp_path, trigger):
    tool, native, apple = make_tool(tmp_path, native=LocationApple())
    identifier = apple.search_locations('虚构公寓')['candidates'][0]['place_id']
    args = reminder({'place_id': identifier, 'trigger': trigger, 'radius': 300})
    args['fields']['due'] = '2027-02-19T13:00:00+08:00'
    args['fields']['early_reminder_minutes'] = 60
    execution = resolve(tool, args)
    assert execution.need_confirm and not native.writes
    summary = execution.approval_preview['summary']
    assert '虚构市测试街45号' in summary and '300米' in summary
    assert ('到达时' if trigger == 'arrive' else '离开时') in summary
    assert '先满足者' in summary
    saved = asyncio.run(execution.run())
    assert saved['saved'] and saved['item']['location'] == {**PLACE, 'trigger': trigger, 'radius': 300}
    assert native.writes[0][1]['fields']['early_reminder_minutes'] == 60
    assert 'latitude' not in args['fields']['location'], '解析不得修改模型原始参数'


@pytest.mark.parametrize('reference', [
    {'place_id': 'invented', 'trigger': 'arrive'},
    {'place_id': 'snapshot', 'trigger': 'arrive'},
    {'place_id': 'PLACE', 'trigger': 'none'},
    {'place_id': 'PLACE', 'trigger': 'arrive', 'radius': True},
    {'place_id': 'PLACE', 'trigger': 'arrive', 'radius': 99},
    {'place_id': 'PLACE', 'trigger': 'arrive', 'latitude': 1},
])
def test_invalid_location_cannot_reach_native_write(tmp_path, reference):
    _, native, apple = make_tool(tmp_path, native=LocationApple())
    identifier = apple.search_locations('虚构公寓')['candidates'][0]['place_id']
    reference = {**reference, 'place_id': identifier if reference['place_id'] == 'PLACE' else reference['place_id']}
    with pytest.raises(ValueError):
        apple.resolve_location(reference)
    assert not native.writes


def test_clear_location_and_unrelated_edit_preserve_other_fields(tmp_path):
    tool, native, apple = make_tool(tmp_path, native=LocationApple())
    native.items[0]['location'] = {**PLACE, 'trigger': 'arrive', 'radius': 200}
    native.items[0]['due'] = '2027-02-19T13:00:00+08:00'
    native.items[0]['early_reminder_minutes'] = 60
    current = apple.detail('reminder', 'reminder-001')
    args = {'action': 'update', 'kind': 'reminder', 'id': current['id'],
            'expected_revision': current['revision'], 'fields': {'title': '改名'}}
    execution = resolve(tool, args)
    assert '到达时' in execution.approval_preview['summary']
    result = asyncio.run(execution.run())
    current = apple.detail('reminder', current['id'])
    assert current['location']['trigger'] == 'arrive'
    args.update(expected_revision=current['revision'], fields={'location': None})
    execution = resolve(tool, args, call_id='clear')
    assert '地点提醒：清除' in execution.approval_preview['summary']
    result = asyncio.run(execution.run())
    assert result['item']['location'] is None
    assert result['item']['due'] == current['due'] and result['item']['early_reminder_minutes'] == 60


def test_maps_failure_does_not_write_or_invent_coordinates(tmp_path):
    tool, native, _ = make_tool(tmp_path, native=LocationApple())
    def fail(action, args):
        raise AppleNativeError('地图暂不可用', code='read_failed')
    native.request = fail
    execution = resolve(tool, reminder('虚构地点'))
    with pytest.raises(AppleNativeError):
        asyncio.run(execution.run())
    assert not native.writes
