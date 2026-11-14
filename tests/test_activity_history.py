import asyncio
import json
import sqlite3
from urllib.parse import unquote, urlsplit, parse_qs

import pytest

from activity_history import ActivityHistory, create_activity_tool, timestamp, summarize
from zoneinfo import ZoneInfo

START = '2026-11-05T00:00:00+08:00'
END = '2026-11-06T00:00:00+08:00'
NOW = timestamp('2026-11-06T12:00:00+08:00')


def fixture(tmp_path):
    db = tmp_path / 'screen.db'
    with sqlite3.connect(db) as c:
        c.executescript('''
            CREATE TABLE devices(device_id TEXT,name TEXT,platform TEXT);
            CREATE TABLE screen_events(event_key TEXT,device_id TEXT,started_at_utc TEXT,duration_s REAL,bundle_id TEXT,title TEXT,category TEXT);
            CREATE TABLE runs(id INTEGER,finished_at TEXT,status TEXT,error TEXT);
            INSERT INTO devices VALUES('phone','Phone','iphone'),('tablet','Tablet','ipad'),('mac','Mini','mac');
            INSERT INTO runs VALUES(1,'2026-11-06T03:00:00Z','success',NULL);
            INSERT INTO screen_events VALUES
                ('phone1','phone','2026-11-04T15:55:00Z',600,'app.a','App A','work'),
                ('phone2','phone','2026-11-05T00:00:00Z',600,'app.b','App B','work'),
                ('phone3','phone','2026-11-05T00:05:00Z',600,'app.b','App B','work'),
                ('tablet1','tablet','2026-11-05T00:00:00Z',600,'app.c','App C','work'),
                ('mac1','mac','2026-11-05T00:00:00Z',3600,'app.d','App D','work');
        ''')
    config = {'health_enabled': False, 'screen_time_db': str(db), 'devices': [
        {'id': 'iphone', 'name': 'Phone', 'aliases': ['phone'], 'screen_time_id': 'phone'},
        {'id': 'ipad', 'name': 'Tablet', 'screen_time_id': 'tablet'},
        {'id': 'mini', 'name': 'Mini', 'aw_hostname': 'mini.local', 'screen_time_id': 'mac'},
        {'id': 'air', 'name': 'Air', 'aw_hostname': 'air.local'},
    ]}
    def event(start, duration, data, id=1):
        return {'timestamp': start, 'duration': duration, 'data': data, 'id': id}
    buckets = {f'{kind}-{host}': {'hostname': host+'.local', 'type': typ}
               for host in ('mini', 'air') for kind, typ in [('window', 'currentwindow'), ('afk', 'afkstatus')]}
    buckets['chrome-air'] = {'hostname': 'air.local', 'type': 'web.tab.current'}
    events = {
        'window-mini': [event('2026-11-05T08:00:00+08:00', 1200, {'app': 'Editor', 'title': 'External title'})],
        'afk-mini': [event('2026-11-05T08:00:00+08:00', 600, {'status': 'not-afk'})],
        'window-air': [event('2026-11-05T08:05:00+08:00', 1200, {'app': 'Google Chrome', 'title': 'Browser'})],
        'afk-air': [event('2026-11-05T08:05:00+08:00', 1200, {'status': 'not-afk'})],
        'chrome-air': [event('2026-11-05T08:00:00+08:00', 3600, {'url': 'https://example.com/private?token=secret', 'title': 'Page'})],
    }
    calls = []
    def get(path):
        calls.append(path)
        if path == '/buckets/': return buckets
        u = urlsplit(path); bid = unquote(u.path.split('/')[2]); q = parse_qs(u.query)
        rows = events[bid]
        if q['limit'] == ['1']: return rows[-1:]
        a, b = timestamp(q['start'][0]), timestamp(q['end'][0])
        return [e for e in rows if timestamp(e['timestamp']) < b and timestamp(e['timestamp'])+e['duration'] > a]
    return config, get, calls, events, buckets


def test_phone_only_overlap_clipping_union_and_pagination(tmp_path):
    config, get, calls, *_ = fixture(tmp_path)
    r = ActivityHistory(None, config, now=lambda: NOW, aw_get=get).query(
        {'devices': ['phone'], 'start': START, 'end': END, 'limit': 1})
    assert [d['id'] for d in r['devices']] == ['iphone']
    assert r['summary']['personal_observed_active_minutes'] == 20
    assert r['summary']['devices'][0]['records'] == 3
    assert r['activities_total'] == 3 and len(r['activities']) == 1
    assert r['activities_next_offset'] == 1
    assert r['activities'][0]['duration_seconds'] == 300
    assert r['summary']['late_night_observed_minutes'] == 5
    assert calls == ['/buckets/']  # no other device's events queried
    assert r['status'] == 'partial'  # latest observation is not coverage


def test_same_device_preference_cross_device_union_and_afk_pairing(tmp_path):
    config, get, *_ = fixture(tmp_path)
    reader = ActivityHistory(None, config, now=lambda: NOW, aw_get=get)
    r = reader.query({'devices': ['mini', 'air'], 'start': START, 'end': END})
    assert r['summary']['devices'] == [
        {'device_id': 'mini', 'source': 'aw', 'observed_active_minutes': 10, 'records': 1},
        {'device_id': 'air', 'source': 'aw', 'observed_active_minutes': 20, 'records': 1}]
    assert r['summary']['device_minutes_sum'] == 30
    assert r['summary']['personal_observed_active_minutes'] == 25
    assert 'secret' not in json.dumps(r)
    assert r['activities'][1]['browser_context'][0]['domain'] == 'example.com'
    assert r['activities'][1]['browser_context'][0]['start'] == '2026-11-05T00:05:00.000+00:00'
    screen = reader.query({'sources': ['screen_time'], 'devices': ['mini'], 'start': START, 'end': END})
    assert screen['summary']['personal_observed_active_minutes'] == 60


def test_unknown_ambiguous_devices_never_fall_back(tmp_path):
    config, get, *_ = fixture(tmp_path)
    reader = ActivityHistory(None, config, aw_get=get)
    with pytest.raises(ValueError, match='设备'): reader.query({'view': 'status', 'devices': ['missing']})
    config['devices'][0]['aliases'].append('mini')
    config['devices'][2]['aliases'] = ['shared']
    config['devices'][0]['aliases'].append('shared')
    with pytest.raises(ValueError, match='歧义'): reader.query({'view': 'status', 'devices': ['shared']})
    assert reader.query({'view': 'status', 'devices': ['mini']})['devices'][0]['id'] == 'mini'


def test_failures_missing_records_and_empty_catalog_are_not_zero(tmp_path):
    config, get, calls, events, buckets = fixture(tmp_path)
    reader = ActivityHistory(None, config, now=lambda: NOW, aw_get=get)
    r = reader.query({'devices': ['iphone'], 'start': '2026-11-06T08:00:00+08:00', 'end': '2026-11-06T12:00:00+08:00'})
    assert r['summary']['personal_observed_active_minutes'] is None
    assert r['sources_status'][1]['state'] == 'missing_records'
    buckets.pop('afk-mini')
    r = reader.query({'devices': ['mini'], 'sources': ['aw'], 'start': START, 'end': END})
    assert r['summary']['personal_observed_active_minutes'] is None
    assert r['sources_status'][0]['state'] == 'missing_buckets'
    def fail(path): raise OSError('unavailable')
    r = ActivityHistory(None, config, now=lambda: NOW, aw_get=fail).query({'start': START, 'end': END})
    assert r['status'] == 'partial'
    assert r['summary']['devices'][0]['observed_active_minutes'] == 20
    r = ActivityHistory(None, {}, aw_get=lambda _: {}).query({'view': 'status', 'sources': ['aw']})
    assert r['sources_status'][0]['state'] == 'missing_devices'


def test_calendar_is_planned_and_not_counted(tmp_path):
    config, get, *_ = fixture(tmp_path)
    class Calendar:
        def query(self, args):
            assert args['kind'] == 'event'
            rows = [{'id': 'e'+str(i), 'start': START, 'end': END, 'event_status': status,
                     'title': 'Planned', 'notes': 'private', 'attendees': ['private']} for i, status in enumerate([1, 3])]
            return {'items': rows[args['offset']:args['offset']+1], 'total': 2}
    r = ActivityHistory(Calendar(), config, now=lambda: NOW, aw_get=get).query({'sources': ['calendar'], 'start': START, 'end': END})
    assert r['planned_events_total'] == 1
    assert r['planned_events'][0]['kind'] == 'planned'
    assert r['summary']['personal_observed_active_minutes'] is None
    assert 'private' not in json.dumps(r)


def test_dst_and_midnight_statistics_are_real_elapsed_time():
    a = timestamp('2026-11-01T00:30:00-04:00'); b = timestamp('2026-11-01T02:30:00-05:00')
    r = summarize([{'start_s': a, 'end_s': b, 'device_id': 'phone', 'app': 'A', 'source':'screen_time', 'evidence_ref':'dst'}], [{'id': 'phone'}], {'phone': 'screen_time'}, a, b, ZoneInfo('America/New_York'), 10)
    assert r['personal_observed_active_minutes'] == 180
    assert r['hourly_observed_minutes'] == [{'hour': 0, 'minutes': 30}, {'hour': 1, 'minutes': 120}, {'hour': 2, 'minutes': 30}]


def test_native_telos_agent_registration_and_execution(tmp_path):
    from support import Harness, call
    config, get, *_ = fixture(tmp_path)
    async def run():
        h = Harness(tmp_path / 'host', [call('history', {'devices': ['iphone'], 'sources': ['screen_time'], 'view': 'summary', 'start': START, 'end': END}, 'activity_history'), '已完成查询。'])
        h.runtime.business.tools['activity_history'] = create_activity_tool(None, config)
        try:
            await h.submit('查询昨天 iPhone 使用记录。')
            results = [e for e in h.events if e['type'] == 'tool/result']
            assert results and '20' in json.dumps(results, ensure_ascii=False)
            assert not h.native.writes
            assert any('activity_history' in json.dumps(req) for req in h.model.requests)
            assert '已完成查询。' in '\n'.join(h.sent)
        finally:
            await h.close()
    asyncio.run(run())


def health_backend():
    calls = []
    events = [
        {'event_id': 'workout:ride', 'kind': 'workout', 'activity_type': 'cycling',
         'start': '2026-11-05T08:00:00+08:00', 'end': '2026-11-05T08:10:00+08:00',
         'duration_seconds': 550, 'source': 'Apple Watch',
         'route': {'event_id': 'route:ride', 'recorded_end': '2026-11-05T08:08:00+08:00'}},
        {'event_id': 'sleep:nap', 'kind': 'sleep', 'activity_type': 'sleep_stage', 'stage': 'deep',
         'start': '2026-11-05T13:00:00+08:00', 'end': '2026-11-05T13:30:00+08:00',
         'duration_seconds': 1800, 'source': 'Apple Watch'},
    ]
    def get(params):
        calls.append(dict(params))
        if params['mode'] == 'detail':
            return {'status': 'ok', 'event': events[0], 'last_sync_at': END, 'pagination': {'next_offset': None}}
        offset = params['offset']
        return {'status': 'ok', 'events': events[offset:offset+1], 'last_sync_at': END,
                'freshness_seconds': 3600, 'notes': ['GPS gaps are unknown.'],
                'pagination': {'next_offset': offset+1 if offset == 0 else None}}
    return get, calls


def test_health_default_combined_timeline_keeps_overlaps_without_counting_health_as_device_time(tmp_path):
    config, get, *_ = fixture(tmp_path)
    config['health_enabled'] = True
    health, calls = health_backend()
    reader = ActivityHistory(None, config, now=lambda: NOW, aw_get=get, health_get=health)
    result = reader.query({'devices': ['iphone'], 'start': START, 'end': END})
    assert 'health' in result['requested_sources']
    assert result['health_events_total'] == 2
    assert result['summary']['personal_observed_active_minutes'] == 20
    assert result['summary_scope'] == 'device_usage_only'
    ride = next(e for e in result['activities'] if e.get('activity_type') == 'cycling')
    assert ride['source'] == 'health' and ride['recording_source'] == 'Apple Watch'
    assert ride['recorded_duration_seconds'] == 550 and ride['duration_seconds'] == 600
    assert ride['route']['event_id'] == 'health:route:ride'
    assert len(calls) == 2
    full = result['activities']
    pages = [reader.query({'devices': ['iphone'], 'start': START, 'end': END, 'offset': i, 'limit': 1})['activities'][0] for i in range(len(full))]
    assert pages == full
    summary = reader.query({'start': START, 'end': END, 'sources': ['health'], 'view': 'summary'})
    assert len(summary['health_events']) == 2
    assert summary['summary']['personal_observed_active_minutes'] is None


def test_health_failures_do_not_hide_device_evidence_or_count_as_no_activity(tmp_path):
    config, get, *_ = fixture(tmp_path)
    config['health_enabled'] = True
    def fail(params):
        raise OSError('offline')
    reader = ActivityHistory(None, config, now=lambda: NOW, aw_get=get, health_get=fail)
    result = reader.query({'devices': ['iphone'], 'start': START, 'end': END})
    assert result['summary']['personal_observed_active_minutes'] == 20
    assert next(s for s in result['sources_status'] if s['source'] == 'health')['state'] == 'unavailable'
    assert result['status'] == 'partial'


def test_health_explicit_filter_status_and_detail_ids(tmp_path):
    config, get, *_ = fixture(tmp_path)
    config['health_enabled'] = True
    health, calls = health_backend()
    reader = ActivityHistory(None, config, now=lambda: NOW, aw_get=get, health_get=health)
    reader.query({'start': START, 'end': END, 'sources': ['screen_time']})
    assert not calls
    status = reader.query({'view': 'status', 'sources': ['health']})
    assert status['sources_status'][0]['scope'] == 'personal_health_not_device_filtered'
    detail = reader.query({'view': 'detail', 'event_id': 'health:workout:ride'})
    assert detail['health_detail']['event']['route']['event_id'] == 'health:route:ride'
    assert calls[-1]['event_id'] == 'workout:ride'
    with pytest.raises(ValueError):
        reader.query({'view': 'timeline', 'event_id': 'health:workout:ride'})
    with pytest.raises(ValueError):
        reader.query({'view': 'detail', 'event_id': 'health:workout:ride', 'devices': ['iphone']})


def test_health_bad_pagination_is_unavailable_not_truncated_success(tmp_path):
    config, get, *_ = fixture(tmp_path)
    config['health_enabled'] = True
    health, _ = health_backend()
    def stuck(params):
        value = health(params)
        value['pagination']['next_offset'] = 0
        return value
    result = ActivityHistory(None, config, now=lambda: NOW, aw_get=get, health_get=stuck).query({'sources': ['health'], 'start': START, 'end': END})
    assert result['sources_status'][0]['state'] == 'unavailable'
    assert result['activities'] == []
    with pytest.raises(ValueError, match='本机'):
        ActivityHistory(None, {'health_url': 'http://example.com'})


def test_title_evidence_is_bounded_deduplicated_and_retains_earlier_period():
    from activity_history import activity_evidence
    def row(start, end, title, ref, source='aw'):
        return dict(start_s=timestamp(start),end_s=timestamp(end),device_id='air',source=source,app='WPS',title=title,evidence_ref=ref)
    rows=[row('2026-11-09T19:30:00+08:00','2026-11-09T19:32:00+08:00','昨天的材料','old'),
          row('2026-11-10T14:00:00+08:00','2026-11-10T14:15:00+08:00','文字文稿3','a'),
          row('2026-11-10T14:05:00+08:00','2026-11-10T14:15:00+08:00','文字文稿3','b'),
          row('2026-11-10T14:15:00+08:00','2026-11-10T14:18:00+08:00','论文评议书.pdf','c'),
          row('2026-11-10T14:18:00+08:00','2026-11-10T14:19:00+08:00','','d','screen_time')]
    result=activity_evidence(rows,ZoneInfo('Asia/Shanghai'),2)
    assert len(result['activity_evidence'])==2
    assert result['activity_evidence'][0]['title']=='昨天的材料'
    assert result['activity_evidence'][1]['title_detail']=='generic'
    assert result['activity_evidence'][1]['observed_minutes']==15
    assert result['activity_evidence_omitted_groups']==2
    full=activity_evidence(rows,ZoneInfo('Asia/Shanghai'),10)['activity_evidence']
    assert next(x for x in full if x['title']=='')['title_detail']=='unavailable'
    assert next(x for x in full if x['title']=='论文评议书.pdf')['title_detail']=='specific'
