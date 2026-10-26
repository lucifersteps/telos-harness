"""The compiled delivery path keeps lists intact through CLI and Desktop hosts."""
import pytest

from settings import read_config, write_config
from test_desktop import Desktop, DESKTOP
from test_package import ARCHIVE, Installed, run_async


@pytest.mark.parametrize('desktop', [False, True])
@pytest.mark.skipif(DESKTOP is None or not ARCHIVE.exists(), reason='Requires built archive and official Desktop runtime')
@run_async
async def test_packaged_lists_stay_together_after_restart(tmp_path, desktop):
    notes='**笔记（2 篇）**\n- 研究演讲.md\n  - 演讲结构\n- 金字塔原理.md'
    references='参考资料：\n\n1. 演讲幻灯片.pdf\n2. 金字塔原理.pdf'
    reply='这次读到了。\n\n'+notes+'\n\n'+references+'\n\n以上是相关资料。'
    expected=['这次读到了。', notes, references, '以上是相关资料。']
    h=Desktop(tmp_path) if desktop else Installed(tmp_path, [])
    try:
        if desktop:
            await h.start()
            await h.action('save', tools={'apple':False, 'maps':False})
            config=read_config(h.home/'telos')
            config['wechat'].update(base_url=h.services.url, cdn_base_url=h.services.url)
            write_config(h.home/'telos', config)
            await h.action('login')
            await h.action('start')
        else:
            h.run('--telos-login')
            h.start()
        h.model.script=[reply, reply]
        for attempt in range(2):
            h.services.push('list-'+str(attempt), '列出相关笔记')
            await h.wait(lambda:len(h.services.sent)==len(expected)*(attempt+1))
            sent=[item['msg']['item_list'][0]['text_item']['text'] for item in h.services.sent]
            assert sent==expected*(attempt+1)
            if attempt==0:
                h.stop()
                if desktop:
                    await h.start()
                    await h.wait(lambda:h.get()['running'])
                else:
                    h.start()
        assert not h.services.apple.writes
    finally:
        await h.close()
