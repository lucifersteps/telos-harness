import json
from pathlib import Path
import subprocess

import pytest
from apple_component import AppleAppRunner, apple_client
from telos.connectors.apple.native import AppleNativeError
from telos.tools.apple.service import AppleService


def test_launchservices_private_transport_and_cleanup(tmp_path,monkeypatch):
    helper=tmp_path/'Telos Apple.app/Contents/MacOS/telos-apple'
    helper.parent.mkdir(parents=True);helper.touch()
    calls=[];transport=[]
    def run(argv,**kwargs):
        calls.append(argv)
        assert argv[:6]==['/usr/bin/open','-n','-W','-g','-j','-a']
        assert argv[6]==str(helper.parents[2])
        paths=[Path(argv[argv.index(key)+1]) for key in ['--stdin','--stdout','--stderr']]
        transport.extend(paths)
        assert all(p.stat().st_mode&0o777==0o600 for p in paths)
        assert paths[0].parent.stat().st_mode&0o777==0o700
        request=json.loads(paths[0].read_text())
        assert 'private-note' not in str(argv)
        paths[1].write_text(json.dumps({'ok':True,'data':{'action':request['action']}}))
        assert kwargs['timeout']==70
        return subprocess.CompletedProcess(argv,0,'','')
    monkeypatch.setattr(subprocess,'run',run)
    client=apple_client({},helper)
    for action in ['status','authorize','snapshot','create']:
        assert client.request(action,{'notes':'private-note'})=={'action':action}
    assert len(calls)==4 and all(not p.exists() for p in transport)


@pytest.mark.parametrize('mode',['timeout','empty','bad-json','permission'])
def test_failures_preserve_unknown_write_protection(tmp_path,monkeypatch,mode):
    helper=tmp_path/'Telos Apple.app/Contents/MacOS/telos-apple'
    helper.parent.mkdir(parents=True);helper.touch()
    calls=[];files=[]
    def run(argv,**kwargs):
        calls.append(argv);reply=Path(argv[argv.index('--stdout')+1]);files.append(reply)
        if mode=='timeout':raise subprocess.TimeoutExpired(argv,kwargs['timeout'])
        reply.write_text({'empty':'','bad-json':'{','permission':json.dumps({'ok':False,'error':{'code':'permission_denied','message':'请授权 Telos Apple。'}})}[mode])
        return subprocess.CompletedProcess(argv,0,'','')
    monkeypatch.setattr(subprocess,'run',run)
    service=AppleService(apple_client({},helper),tmp_path/'ledger')
    args={'kind':'event','fields':{'title':'fictional'}}
    with pytest.raises(AppleNativeError):service.mutate('create',args,'confirmed-operation')
    with service.database() as db:row=db.execute('SELECT status FROM operations').fetchone()
    if mode=='permission':assert row is None  # Explicitly denied before any save.
    else:
        assert row[0]=='pending'
        with pytest.raises(AppleNativeError,match='未知'):service.mutate('create',args,'confirmed-operation')
        assert len(calls)==1  # Lost receipt is never silently replayed.
    assert all(not p.exists() for p in files)


def test_override_and_no_daemon(tmp_path):
    helper=tmp_path/'fake';helper.touch()
    assert not isinstance(apple_client({'apple_helper':str(helper)},helper).runner,AppleAppRunner)
    with pytest.raises(ValueError):AppleAppRunner().run([str(helper)],input='{}',timeout=60)
