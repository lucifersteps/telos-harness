"""DSH pressure hook with a local model, including desktop service scope."""
import asyncio
import json
import pytest
from support import Harness
from runtime import ROOT

@pytest.mark.parametrize('host_backend', [True, False])
def test_native_compaction_and_resume(tmp_path, host_backend):
    async def run():
        h = Harness(tmp_path, [])
        def large_reply(request):
            h.model.input_tokens = 800000
            return 'A' * 600000
        def summary(request):
            h.model.input_tokens = 10
            return 'The user supplied a long test record. Continue with the next request.'
        h.model.script = [large_reply, summary, '压缩后继续。', '恢复后继续。']
        if not host_backend:
            patch = tmp_path / 'desktop-compaction.patch.yml'
            patch.write_text((ROOT / 'native.patch.yml').read_text() + '\n- id: compaction-basic\n  disabled: true\n')
            h.runtime.command[-1] = str(patch)
        try:
            await h.submit('B' * 600000)
            await h.submit('继续处理')
            assert h.sent[-1] == '压缩后继续。'
            assert len([e for e in h.events if e['type'] == 'compaction/start']) == 1
            assert len([e for e in h.events if e['type'] == 'compaction/summary']) == 1
            ends = [e for e in h.events if e['type'] == 'compaction/end']
            assert len(ends) == 1 and not ends[0]['data'].get('error')
            assert len(json.dumps(h.model.requests[-1])) < len(json.dumps(h.model.requests[1]))
            await h.runtime.close()
            h.runtime = h.restart()
            if not host_backend:
                h.runtime.command[-1] = str(patch)
            await h.submit('重启后继续')
            assert h.sent[-1] == '恢复后继续。'
            assert 'long test record' in json.dumps(h.model.requests[-1])
        finally:
            await h.close()
    asyncio.run(run())
