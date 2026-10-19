"""同样的 18 项真实模型评测，入口改为已安装插件的微信轮询。"""
import asyncio
import json
import os
from pathlib import Path
import sqlite3
import time
import pytest
from test_live import CASES,connection
from test_package import Installed,ARCHIVE

@pytest.mark.skipif(not os.getenv('TELOS_PACKAGE_LIVE') or not ARCHIVE.exists(),reason='真实安装包评测须显式启用')
@pytest.mark.parametrize('repetition',range(3))
@pytest.mark.parametrize('case',CASES,ids=[c[0] for c in CASES])
def test_live_installed_mixed_request_coverage(tmp_path,case,repetition):
    async def run():
        h=Installed(tmp_path,[],connection=connection())
        h.services.apple.items[0]['title']='买测试用橙色文件夹'
        h.services.apple.items.append({'id':'existing-event','kind':'event','calendar_id':'calendar-event',
            'title':'虚构星河研讨','start':'2027-02-19T09:00:00+08:00','end':'2027-02-19T10:00:00+08:00'})
        name,prompt,count,names=case
        row={'case':name,'repetition':repetition};start=time.perf_counter()
        try:
            h.run('--telos-login');h.start();h.services.push('input','以下全部是虚构评测数据。'+prompt)
            def finished():
                if not (h.data/'business.sqlite').exists():return False
                with sqlite3.connect(h.data/'business.sqlite') as db:
                    return db.execute('SELECT count(*) FROM messages WHERE delivered=1').fetchone()[0]>0
            await h.wait(finished,timeout=180)
            with sqlite3.connect(h.data/'business.sqlite') as db:
                rows=db.execute('SELECT value FROM pending').fetchall()
                pending=json.loads(rows[0][0]) if rows else {}
                text='\n'.join(r[0] or '' for r in db.execute('SELECT reply FROM messages').fetchall())
            args=pending.get('args',{});operations=args.get('operations',[args] if args else [])
            previews=[op for op in operations if op.get('action')=='create']
            row.update(preview_ok=pending.get('kind')=='approval' and len(previews)==count and
                any(op.get('fields',{}).get('title')=='虚构评测会议甲' for op in previews),
                answer_ok=all(n in text for n in names),no_unapproved_writes=not h.services.apple.writes,reply=text)
            assert row['preview_ok'] and row['answer_ok'] and row['no_unapproved_writes'],row
        finally:
            row['seconds']=time.perf_counter()-start
            with Path(os.environ['TELOS_NATIVE_REPORT']).open('a') as f:f.write(json.dumps(row,ensure_ascii=False)+'\n')
            await h.close()
    asyncio.run(run())
