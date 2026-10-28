import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

function page({dshVersion='0.2.1-rc.3',desktop=false}={}) {
  const states=[],effects=[],requests=[],credentialWrites=[],signIns=[];let cursor=0,first=true,plugin,slot;
  const status={configuration:true,running:false,dshVersion,config:{max_steps:24,persona:'default',persona_file:''},personas:[{id:'default',label:'默认助手'},{id:'liman',label:'黎曼'}]};
  const credentials={describe:async(refs)=>({ok:true,value:Object.fromEntries(refs.map(ref=>[ref,{configured:credentialWrites.some(w=>w.ref===ref),writable:true}]))}),
    set:async(ref,value)=>{credentialWrites.push({ref,value});return {ok:true};}};
  const React={createElement:(type,props,...children)=>({type,props:props||{},children}),
    useState:initial=>{const index=cursor++;if(!(index in states))states[index]=initial;return [states[index],value=>{states[index]=typeof value==='function'?value(states[index]):value;}];},
    useRef:value=>{const index=cursor++;if(!(index in states))states[index]={current:value};return states[index];},
    useEffect:fn=>{if(first)effects.push(fn);}};
  vm.runInNewContext(readFileSync(new URL('../plugin/client.js',import.meta.url),'utf8'),{
    window:{location:{origin:'http://localhost:1234'},__ModuleLoader__:{load:({factory})=>{plugin=factory(()=>React);}}},
    ...(desktop?{dshDesktop:{},__DSH_TRANSPORT__:{streamBaseUrl:'http://localhost:5678/api/remote.mux'}}:{}),
    fetch:async(url,options)=>{requests.push({url,options});return {ok:true,json:async()=>url.endsWith('/models')?{providers:[{id:'api',name:'API',models:[{id:'test-model',name:'Test model'}],credentialRef:'TEST_API_KEY'}]}:options?.method?{accepted:true}:status};},
    setInterval:()=>1,clearInterval:()=>{},AbortController,URL});
  // Match Cordis' declared-service access guard, including parent services.
  const services={remote:{credentials,account:{startSignIn:async(...args)=>{signIns.push(args);return {ok:true};}}},slots:{inject:(_name,fn)=>fn(),register:(_selector,render)=>{slot=render;}}};
  const context=new Proxy(services,{get(target,key){
    if(key==='inject')return (_dependencies,fn)=>fn({remote:services.remote,effect:()=>{}});
    if(!plugin.inject.includes(key))throw new Error(`cannot get property ${String(key)} without inject`);
    return target[key];
  }});
  plugin.apply(context);
  function render(){cursor=0;const node=slot();const tree=node.type(node.props);first=false;return tree;}
  function nodes(tree){return [tree,...tree.children.flat(Infinity).filter(v=>v&&typeof v==='object').flatMap(nodes)];}
  function find(tree,predicate){const result=nodes(tree).find(predicate);assert.ok(result);return result;}
  return {render,find,status,requests,credentialWrites,credentials,effects,plugin,signIns};
}
async function mounted(options){const p=page(options);p.render();for(const effect of p.effects)effect();await new Promise(resolve=>setImmediate(resolve));return p;}

test('starting after selecting persona submits that choice, not saved default',async()=>{
  const p=await mounted();let tree=p.render();
  p.find(tree,n=>n.props['aria-label']==='微信人设').props.onChange({target:{value:'liman'}});tree=p.render();
  assert.ok(p.find(tree,n=>n.type==='p'&&n.children.some(v=>typeof v==='string'&&v.includes('未保存'))));
  await p.find(tree,n=>n.type==='button'&&n.children.includes('启动 Telos')).props.onClick();
  const body=JSON.parse(p.requests.at(-1).options.body);
  assert.equal(body.action,'start');assert.equal(body.persona,'liman');
});

test('map credential goes directly to DSH and never to Telos requests',async()=>{
  const p=await mounted();let tree=p.render();
  assert.ok(p.plugin.inject.includes('remote.credentials'));
  p.find(tree,n=>n.props['aria-label']==='百度地图 Token').props.onChange({target:{value:'fictional-map-token'}});
  tree=p.render();await p.find(tree,n=>n.type==='button'&&n.children.includes('保存到 DSH')).props.onClick();
  assert.deepEqual(p.credentialWrites,[{ref:'BAIDU_MAP_AUTH_TOKEN',value:'fictional-map-token'}]);
  tree=p.render();assert.equal(p.find(tree,n=>n.props['aria-label']==='百度地图 Token').props.value,'');
  assert.ok(!JSON.stringify(p.requests).includes('fictional-map-token'));
  assert.ok(p.find(tree,n=>n.type==='p'&&n.children.some(v=>typeof v==='string'&&v.includes('DSH 已配置'))));
});

test('credential failure stays unsaved and does not echo a secret',async()=>{
  const p=await mounted();p.credentials.set=async()=>{throw new Error('fictional-secret');};
  let tree=p.render();p.find(tree,n=>n.props['aria-label']==='百度地图 Token').props.onChange({target:{value:'fictional-secret'}});
  tree=p.render();await p.find(tree,n=>n.type==='button'&&n.children.includes('保存到 DSH')).props.onClick();
  tree=p.render();assert.ok(p.find(tree,n=>n.type==='p'&&n.children.some(v=>typeof v==='string'&&v.includes('未能保存'))));
  assert.ok(!JSON.stringify(p.requests).includes('fictional-secret'));
});


test('model and tool changes persist only nonsecret choices, using apply',async()=>{
  const p=await mounted();let tree=p.render();
  p.find(tree,n=>n.props['aria-label']==='模型来源').props.onChange({target:{value:'api'}});
  tree=p.render();p.find(tree,n=>n.props['aria-label']==='百度地图').props.onChange({target:{checked:false}});
  tree=p.render();p.find(tree,n=>n.props['aria-label']==='模型 API Key').props.onChange({target:{value:'fictional-model-key'}});
  tree=p.render();await p.find(tree,n=>n.type==='button'&&n.children.includes('保存到 DSH')).props.onClick();
  tree=p.render();await p.find(tree,n=>n.type==='button'&&n.children.includes('保存并应用')).props.onClick();
  const body=JSON.parse(p.requests.at(-1).options.body);
  assert.equal(body.action,'apply');assert.equal(body.model_selection.provider,'api');assert.equal(body.model_selection.model,'test-model');
  assert.equal(body.tools.maps,false);assert.equal(body.tools.apple,true);
  assert.deepEqual(p.credentialWrites,[{ref:'TEST_API_KEY',value:'fictional-model-key'}]);
  assert.ok(!JSON.stringify(p.requests).includes('fictional-model-key'));
});

test('direct write defaults on and can be disabled through the plugin UI',async()=>{
  const p=await mounted();let tree=p.render();
  const toggle=p.find(tree,n=>n.props['aria-label']==='直接写入');
  assert.equal(toggle.props.checked,true);
  toggle.props.onChange({target:{checked:false}});
  tree=p.render();await p.find(tree,n=>n.type==='button'&&n.children.includes('保存并应用')).props.onClick();
  assert.equal(JSON.parse(p.requests.at(-1).options.body).direct_write,false);
});

for(const dshVersion of ['0.2.1-rc.2','0.2.1-rc.3'])for(const desktop of [false,true]){
  test(`account sign-in sends the running DSH ${dshVersion} version on ${desktop?'desktop':'web'}`,async()=>{
    const p=await mounted({dshVersion,desktop});
    await p.find(p.render(),n=>n.type==='button'&&n.children.includes('登录 DeepSeek 账号')).props.onClick();
    assert.equal(p.signIns.length,1);
    const [metadata,origin,platform]=p.signIns[0];
    assert.equal(metadata.version,dshVersion);
    assert.equal(origin,desktop?'http://localhost:5678':'http://localhost:1234');
    assert.equal(platform,desktop?'desktop':'web');
  });
}

test('account sign-in reports a missing host version without inventing one',async()=>{
  const p=await mounted({dshVersion:null});
  await p.find(p.render(),n=>n.type==='button'&&n.children.includes('登录 DeepSeek 账号')).props.onClick();
  assert.equal(p.signIns.length,0);
  assert.ok(p.find(p.render(),n=>n.type==='p'&&n.children.some(v=>typeof v==='string'&&v.includes('无法读取 DSH 宿主版本'))));
});
