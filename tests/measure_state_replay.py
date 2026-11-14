"""Offline replay comparison; exports counts only, never private reasoning."""
from compression import zstd
from pathlib import Path
import json,os,subprocess,sys
sys.path.insert(0,os.environ.get('TELOS_TOKENIZER_MODULES','/private/tmp/telos-token-analysis-deps'))
from tokenizers import Tokenizer

ROOT=Path(__file__).resolve().parents[1]
report=ROOT/'reports/current-state-20261104';report.mkdir(exist_ok=True)
source=Path(os.environ['TELOS_AUDIT_SESSION_LOG'])
with zstd.open(source,'rt') as f:events=[e for line in f if 'seq' in (e:=json.loads(line)) and e['seq']<7259]
app=Path(os.environ['TELOS_TEST_DESKTOP_APP'])/'Contents'
env={**os.environ,'ELECTRON_RUN_AS_NODE':'1','DSH_NATIVE_ROOT':str(app/'Resources/app.asar/dsh/node_modules/@deepseek-ai')}
r=subprocess.run([str(app/'MacOS/DeepSeek Harness'),'--preserve-symlinks',str(ROOT/'tests/state-replay.cjs')],
 input=json.dumps({'events':events,'at':'2026-11-04T18:52:22+08:00'}),capture_output=True,text=True,env=env)
if r.returncode:raise RuntimeError(r.stderr[-2000:])
data=json.loads(r.stdout);tokenizer=Tokenizer.from_file('/private/tmp/telos-v41-tokenizer.json')
count=lambda texts:sum(len(tokenizer.encode(t,add_special_tokens=False).ids)for t in texts)
removed=count(data.pop('removed_texts'));added=count(data.pop('added_texts'))
data.update(baseline_input=345535,removed_text_tokens=removed,added_text_tokens=added,
 estimated_after_input=345535-removed+added,estimated_reduction_pct=round((removed-added)/345535*100,2),
 method='same retained session prefix before seq 7259; native Session replacement; text tokenizer delta; message wrapper differences excluded')
(report/'offline-replay.json').write_text(json.dumps(data,ensure_ascii=False,indent=2))
print(json.dumps(data,ensure_ascii=False,indent=2))
