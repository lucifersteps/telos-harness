"""DSH owns context deduplication; Apple reads stay in business tools."""
import asyncio
import json
from business import Business
from support import Harness, call


def test_context_never_reads_apple_and_preserves_receipt(tmp_path):
    class Forbidden:
        def __getattr__(self, name):
            raise AssertionError('unexpected Apple access: ' + name)
    async def run():
        b = Business(tmp_path, Forbidden())
        b.active['s'] = {'time': '2026-10-19T18:00:00+08:00', 'receipt': {'text': '已创建成功。'}}
        b.set_pending('s', {'kind': 'question', 'question': '哪个清单？'})
        first = await b.context('s')
        assert first == await b.context('s')
        assert first['receipt']['text'] == '已创建成功。'
        assert first['pending']['question'] == '哪个清单？'
        assert 'external_state' not in first
    asyncio.run(run())


def test_native_context_dedup_and_fresh_tool_reads(tmp_path):
    async def run():
        h = Harness(tmp_path, [call('r1', {'action': 'query', 'kind': 'reminder'}),
                              call('r2', {'action': 'query', 'kind': 'reminder'}), '查询完成。'])
        reads = []
        request = h.native.request
        def track(action, args):
            if action == 'snapshot':
                reads.append(action)
                h.native.items[0]['title'] = '当前事项-' + str(len(reads))
            return request(action, args)
        h.native.request = track
        try:
            await h.submit('连续核对最新提醒事项')
            contexts = [e for e in h.events if e['type'] == 'user/message'
                        and e['data'].get('source', {}).get('kind') == 'runtime-context']
            assert len(contexts) == 1
            assert len(reads) == 2
            snapshot = json.dumps(contexts[0], ensure_ascii=False)
            assert '当前事项-' not in snapshot and 'checked_at' not in snapshot
            results = [e for e in h.events if e['type'] == 'tool/result']
            assert '当前事项-1' in json.dumps(results[0], ensure_ascii=False)
            assert '当前事项-2' in json.dumps(results[1], ensure_ascii=False)
            assert h.sent[-1] == '查询完成。'
        finally:
            await h.close()
    asyncio.run(run())
