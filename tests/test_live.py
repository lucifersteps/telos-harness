"""与 Telos 基线相同的六类虚构输入、三次重复及评分规则。"""
import asyncio,json,os,re,time
from pathlib import Path
import pytest
from dotenv import dotenv_values
from support import Harness

CASES = [
    ('direct', '请创建“虚构评测会议甲”，2027年2月19日14点到15点。另外查询提醒事项，告诉我具体有哪些。', 1, ['买测试用橙色文件夹']),
    ('aside', '我打算2027年2月19日14点到15点开“虚构评测会议甲”，帮我加日历。对了，顺便看看我还有什么提醒事项，把名字告诉我。', 1, ['买测试用橙色文件夹']),
    ('read-first', '先告诉我有哪些提醒事项，再创建“虚构评测会议甲”，时间是2027年2月19日14点到15点。', 1, ['买测试用橙色文件夹']),
    ('batch', '创建两个日程：“虚构评测会议甲”2027年2月19日14–15点、“虚构评测会议乙”2月20日10–11点，都在测试室。还要告诉我现在有哪些提醒事项。', 2, ['买测试用橙色文件夹']),
    ('two-reads', '创建“虚构评测会议甲”，2027年2月19日14–15点；并查询当天已有日程和全部提醒事项，分别告诉我名称。', 1, ['买测试用橙色文件夹', '虚构星河研讨']),
    ('constraints', '把“虚构评测会议甲”放在2027年2月19日14点到15点，地点测试楼，备注带材料，提前30分钟提醒。另外，我需要知道目前未完成的提醒事项名称，别忘了回答这一项。', 1, ['买测试用橙色文件夹']),
]

def connection():
    home=Path(os.environ['TELOS_NATIVE_MODEL_HOME'])
    providers=json.loads((home/'config/personal.json').read_text())['providers']
    spec=providers[providers['default']]
    values={**dotenv_values(home/'.env'),**os.environ}
    def resolve(value):
        return re.sub(r'\{([A-Za-z_][A-Za-z_0-9]*)\}',lambda m:values[m[1]],value)
    endpoint=resolve(spec['base_url']).rstrip('/')
    if endpoint in ('https://api.deepseek.com','https://api.deepseek.com/v1'):
        endpoint='https://api.deepseek.com/anthropic'
    return {'model':spec['model'],'api_key':resolve(spec['api_key']),'base_url':endpoint}

@pytest.mark.skipif(not os.getenv('TELOS_NATIVE_MODEL_HOME'),reason='真实 API 评测须显式启用')
@pytest.mark.parametrize('repetition',range(3))
@pytest.mark.parametrize('case',CASES,ids=[c[0] for c in CASES])
def test_live_mixed_request_coverage(tmp_path,case,repetition):
    async def run():
        h=Harness(tmp_path,[],connection=connection())
        h.native.items[0]['title']='买测试用橙色文件夹'
        h.native.items.append({'id':'existing-event','kind':'event','calendar_id':'calendar-event',
            'title':'虚构星河研讨','start':'2027-02-19T09:00:00+08:00','end':'2027-02-19T10:00:00+08:00'})
        name,prompt,count,names=case
        row={'case':name,'repetition':repetition,'model':h.connection['model'],'prompt':prompt}
        start=time.perf_counter()
        try:
            await h.submit('以下全部是虚构评测数据。'+prompt)
            text='\n'.join(h.sent);pending=h.runtime.business.pending(h.session) or {};args=pending.get('args',{})
            operations=args.get('operations',[args] if args else [])
            previews=[op for op in operations if op.get('action')=='create']
            preview_ok=(pending.get('kind')=='approval' and len(previews)==count and
                any(op.get('fields',{}).get('title')=='虚构评测会议甲' for op in previews))
            answer_ok=all(n in text for n in names)
            row.update(preview_ok=preview_ok,answer_ok=answer_ok,reply=text,no_unapproved_writes=not h.native.writes,
                trace=[e for e in h.events if e['type'] in ('tool/call','tool/result','todo/write','turn/end')],
                usage=[e['data'].get('usage',{}) for e in h.events if e['type']=='assistant/message'])
            assert not h.native.writes
            assert preview_ok and answer_ok,json.dumps(row,ensure_ascii=False)
        finally:
            row['seconds']=time.perf_counter()-start
            with Path(os.environ['TELOS_NATIVE_REPORT']).open('a') as f:f.write(json.dumps(row,ensure_ascii=False)+'\n')
            await h.close()
    asyncio.run(run())
