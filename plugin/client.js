window.__ModuleLoader__.load({id:'telos-harness',factory:require=>{
  const React=require('react'),h=React.createElement;
  const defaults={max_steps:24,persona:'default',persona_file:'',model_selection:null,tools:{apple:true,maps:true}};
  const css=`
.telos{--t-bg:var(--dsw-alias-bg-layer-1,#fff);--t-ink:var(--dsw-alias-label-primary,#202624);--t-line:var(--dsw-alias-border-l2,#e2e6e4);color:var(--t-ink);width:100%;max-width:880px;margin:auto;font-size:14px;line-height:1.6}
.telos *{box-sizing:border-box}.telos .t-header{display:flex;justify-content:space-between;align-items:center;gap:16px;margin:8px 0 22px}.telos h2{font-size:25px;letter-spacing:-.7px;margin:0;font-weight:650}.telos h3{font-size:16px;margin:0;font-weight:600}.telos p{margin:6px 0}.telos .t-muted{opacity:.62;font-size:12px}.telos .t-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,310px),1fr));gap:16px}.telos .t-card{border:1px solid var(--t-line);border-radius:16px;padding:22px;background:var(--t-bg);min-width:0}.telos .t-wide{grid-column:1/-1}.telos .t-row{display:flex;align-items:center;gap:10px;flex-wrap:wrap}.telos .t-between{justify-content:space-between}.telos .t-badge{font-size:11px;padding:4px 10px;border-radius:20px;background:rgba(64,125,100,.1);color:#528b71;white-space:nowrap}.telos .t-fields{display:grid;gap:14px;margin-top:18px}.telos label{display:grid;gap:6px;font-size:12px;font-weight:500}.telos input:not([type=checkbox]),.telos select{font:inherit;color:inherit;width:100%;min-width:0;border:1px solid var(--t-line);background:transparent;padding:10px 12px;border-radius:9px;outline-offset:2px}.telos button{font:inherit;font-size:12px;font-weight:550;border:1px solid var(--t-line);border-radius:9px;padding:9px 14px;background:transparent;color:inherit;cursor:pointer;line-height:1.4}.telos button:hover:not(:disabled){background:rgba(100,130,115,.08)}.telos button.t-primary{background:#25634e;color:#fff;border-color:#25634e}.telos button:disabled{opacity:.38;cursor:default}.telos input:disabled,.telos select:disabled{opacity:.55}.telos .t-actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:14px}.telos .t-tool{padding:18px 0;border-top:1px solid var(--t-line);margin-top:18px}.telos .t-switch{display:flex;align-items:center;gap:9px;font-size:13px}.telos input[type=checkbox]{appearance:none;width:34px;height:20px;flex:none;border-radius:20px;background:#a4aaa7;position:relative;cursor:pointer;margin:0}.telos input[type=checkbox]:after{content:'';position:absolute;width:14px;height:14px;top:3px;left:3px;border-radius:50%;background:white;transition:transform .15s}.telos input[type=checkbox]:checked{background:#35785f}.telos input[type=checkbox]:checked:after{transform:translateX(14px)}.telos details{margin-top:16px}.telos summary{cursor:pointer;font-size:12px;opacity:.75}.telos .t-note{border-radius:10px;padding:10px 14px;background:rgba(64,125,100,.07);font-size:12px;margin-top:16px;overflow-wrap:anywhere}.telos .t-error{color:#b34e42;background:rgba(180,60,40,.07)}.telos .t-footer{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;margin:18px 0}.telos .t-token{display:flex;gap:8px;align-items:center;margin-top:12px}.telos .t-token input{flex:1}.telos .t-token button{flex:none}.telos .t-sub{padding:14px 0 0}.telos .t-model-id{overflow-wrap:anywhere}.telos .t-qr{border-radius:12px;background:white;padding:12px;width:220px;margin-top:16px}
`;
  function Setup({credentials,account}) {
    const [state,setState]=React.useState(null),[notice,setNotice]=React.useState(''),[pending,setPending]=React.useState(false);
    const [form,setForm]=React.useState(defaults),[catalog,setCatalog]=React.useState({providers:[]});
    const [keys,setKeys]=React.useState({}),[credentialState,setCredentialState]=React.useState({});
    const [custom,setCustom]=React.useState({name:'',baseURL:'',model:'',api:'openai-completions'});
    const initialized=React.useRef(false);
    async function loadCatalog(){const r=await fetch('api/telos/models');if(!r.ok)throw new Error('无法读取 DSH 模型目录');const v=await r.json();setCatalog(v);return v;}
    async function describe(refs){const r=await credentials.describe(refs);if(!r.ok)throw new Error();setCredentialState(old=>({...old,...r.value}));}
    React.useEffect(()=>{
      let disposed=false;const abort=new AbortController();
      async function refresh(){try {
        const r=await fetch('api/telos/status',{signal:abort.signal});if(!r.ok)throw new Error('无法读取 Telos 状态');
        const value=await r.json();if(disposed)return;setState(value);
        if(!initialized.current){initialized.current=true;if(value.config)setForm({...defaults,...value.config,tools:{...defaults.tools,...value.config.tools}});}
      }catch(e){if(!disposed)setNotice(e.message);}}
      refresh();loadCatalog().then(v=>describe(['BAIDU_MAP_AUTH_TOKEN',...v.providers.map(p=>p.credentialRef).filter(Boolean)])).catch(()=>{if(!disposed)setNotice('无法读取 DSH 模型或凭据状态，请刷新页面重试。');});
      const timer=setInterval(refresh,1500);return ()=>{disposed=true;abort.abort();clearInterval(timer);};
    },[]);
    const blocked=pending||!!state?.busy||!state;
    const saved={...defaults,...state?.config,tools:{...defaults.tools,...state?.config?.tools}};
    const dirty=!!state&&JSON.stringify(form)!==JSON.stringify(saved);
    const selection=form.model_selection;
    const provider=catalog.providers.find(p=>p.id===(selection?.provider||state?.defaultModel?.provider));
    const field=(label,value,onChange,extra={})=>h('label',null,label,h('input',{'aria-label':label,value,onChange:e=>onChange(e.target.value),disabled:blocked,autoComplete:'off',...extra}));
    const select=(label,value,options,onChange)=>h('label',null,label,h('select',{'aria-label':label,value,disabled:blocked,onChange:e=>onChange(e.target.value)},options.map(([v,t])=>h('option',{key:v,value:v},t))));
    const button=(label,fn,disabled=false,primary=false)=>h('button',{type:'button',className:primary?'t-primary':'',disabled:blocked||disabled,onClick:fn},label);
    const small=text=>h('p',{className:'t-muted'},text);
    async function perform(fn){setPending(true);setNotice('');try{await fn();}catch(e){setNotice(e.message);}finally{setPending(false);}}
    async function action(kind){return perform(async()=>{
      const r=await fetch('api/telos/action',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({action:kind,...(['apply','save','start'].includes(kind)?form:{})})});
      const v=await r.json();if(!r.ok)throw new Error(v.error||'操作未完成');setNotice('操作已提交，完成后状态自动更新。');
    });}
    async function modelAction(action,data){const r=await fetch('api/telos/models',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({action,...data})});const v=await r.json();if(!r.ok)throw new Error(v.error||'模型操作未完成');return v;}
    async function saveKey(ref){return perform(async()=>{
      try{const r=await credentials.set(ref,keys[ref].trim());if(!r?.ok)throw new Error();await describe([ref]);setKeys(old=>({...old,[ref]:''}));setNotice('凭据已保存到 DSH，下次调用生效。');}
      catch{throw new Error('DSH 未能保存凭据，请检查宿主凭据服务。');}
    });}
    function credential(ref,label){if(!ref)return null;return h('div',{className:'t-sub'},small(credentialState[ref]?.configured?'DSH 已配置凭据 · 可输入新值替换':'尚未配置凭据'),
      h('div',{className:'t-token'},h('input',{'aria-label':label,type:'password',value:keys[ref]||'',placeholder:label,autoComplete:'off',disabled:blocked||credentialState[ref]?.writable===false,onChange:e=>setKeys(old=>({...old,[ref]:e.target.value}))}),button('保存到 DSH',()=>saveKey(ref),!keys[ref]?.trim()||credentialState[ref]?.writable===false)));}
    const toggle=(key,label)=>h('label',{className:'t-switch'},h('input',{type:'checkbox','aria-label':label,checked:form.tools[key],disabled:blocked,onChange:e=>setForm({...form,tools:{...form.tools,[key]:e.target.checked}})}),label);
    return h('section',{className:'telos'},h('style',null,css),
      h('div',{className:'t-header'},h('div',null,h('h2',null,'Telos'),small('在微信里，把想法变成安排。')),h('span',{className:'t-badge'},state?.running?'运行中':'已停止')),
      h('div',{className:'t-grid'},
        h('div',{className:'t-card t-wide'},h('div',{className:'t-row t-between'},h('div',null,h('h3',null,'微信连接'),small(state?.wechat_login?'已登录 · 聊天历史与待确认操作会保留':'扫码连接你的微信')),h('div',{className:'t-row'},button(state?.wechat_login?'重新扫码':'微信扫码登录',()=>action('login'),state?.running||!state?.configuration),state?.running?button('停止 Telos',()=>action('stop')):button('启动 Telos',()=>action('start'),!state?.wechat_login,true))),
          state?.qr?h('img',{className:'t-qr',src:state.qr,alt:'微信登录二维码'}):null),
        h('div',{className:'t-card'},h('h3',null,'聊天模型'),small('仅选择微信使用的模型，不改变 DSH 默认模型。'),
          h('div',{className:'t-fields'},select('模型来源',selection?.provider||'',[['','跟随 DSH 默认模型'],...catalog.providers.map(p=>[p.id,p.name])],id=>{const p=catalog.providers.find(p=>p.id===id);setForm({...form,model_selection:id?{provider:id,model:p?.models[0]?.id||''}:null});}),
          selection?select('模型',selection.model,[...new Map([...provider?.models?.map(m=>[m.id,m.name])||[],...(selection.model?[[selection.model,selection.model]]:[]),['','请选择模型']]).entries()],id=>setForm({...form,model_selection:{provider:selection.provider,model:id}})):h('p',{className:'t-model-id'},state?.defaultModel?.model||state?.model?.model||'DSH 尚未选择模型'),
          selection?h('details',null,h('summary',null,'手动填写模型 ID / 推理强度'),h('div',{className:'t-fields'},field('模型 ID',selection.model,v=>setForm({...form,model_selection:{...selection,model:v}})),field('推理强度（可留空）',selection.reasoningEffort||'',v=>{const next={...selection};if(v)next.reasoningEffort=v;else delete next.reasoningEffort;setForm({...form,model_selection:next});}))):null),
          credential(provider?.credentialRef,'模型 API Key'),
          selection&&provider?.credentialRef&&!provider.models.length?small('保存 Key 后刷新模型列表，也可手动填写模型 ID。'):null,
          provider?.id==='deepseek-account'||!provider?h('div',{className:'t-actions'},button('登录 DeepSeek 账号',()=>perform(async()=>{
            try{const desktop='dshDesktop' in globalThis,transport=globalThis.__DSH_TRANSPORT__;
              const r=await account.current.startSignIn({version:'0.2.1-rc.2',locale:'zh',timezoneOffsetSeconds:-new Date().getTimezoneOffset()*60},desktop&&transport?.streamBaseUrl?new URL(transport.streamBaseUrl).origin:window.location.origin,desktop?'desktop':'web');if(!r?.ok)throw new Error();setNotice('请在 DSH 原生登录窗口完成登录，再刷新模型列表。');}
            catch{throw new Error('无法打开 DSH 登录，请检查宿主账号服务。');}
          }),!account.current)):null,
          h('div',{className:'t-actions'},button('测试连接',()=>perform(async()=>setNotice((await modelAction('test',{selection})).message))),button('刷新模型',()=>perform(async()=>{const v=await loadCatalog();await describe(v.providers.map(p=>p.credentialRef).filter(Boolean));setNotice('已刷新 DSH 模型目录。');}))),small('测试连接会发送一条测试文字，产生少量调用费用。'),
          catalog.customAvailable?h('details',null,h('summary',null,'添加其他 API 模型'),small('Provider 保存到 DSH，可供其他 DSH 会话选择。'),h('div',{className:'t-fields'},...['name','baseURL','model'].map((key,i)=>field(['显示名称','API 地址','新模型 ID'][i],custom[key],v=>setCustom({...custom,[key]:v}))),select('API 协议',custom.api,[['openai-completions','OpenAI Chat Completions'],['openai-responses','OpenAI Responses'],['anthropic-messages','Anthropic Messages']],v=>setCustom({...custom,api:v}))),h('div',{className:'t-actions'},button('添加到 DSH',()=>perform(async()=>{const added=await modelAction('add',{config:custom});await loadCatalog();await describe([added.credentialRef]);setForm({...form,model_selection:{provider:added.provider,model:added.model}});setNotice('模型已添加到 DSH。请填写上方 API Key，然后测试连接并保存选择。');}),!custom.name.trim()||!custom.model.trim()||!custom.baseURL.trim()))):null),
        h('div',{className:'t-card'},h('h3',null,'微信人设'),small('只影响微信聊天，不影响 DSH 的其他会话。'),h('div',{className:'t-fields'},select('微信人设',form.persona,(state?.personas||[]).map(p=>[p.id,p.label]),v=>setForm({...form,persona:v,persona_file:v==='custom'?form.persona_file:''})),form.persona==='custom'?field('人设文件路径',form.persona_file,v=>setForm({...form,persona_file:v})):null),
          h('div',{className:'t-note'},'当前生效：'+(state?.personas?.find(p=>p.id===state?.config?.persona)?.label||'默认助手')),
          h('div',{className:'t-tool'},h('h3',null,'图片与语音'),small('图片处理和语音识别复用 DSH 原生能力。'),small('本地语音：'+(state?.voice?.providers?.map(p=>({unprepared:'尚未准备',preparing:'准备中',ready:'已就绪',failed:'准备失败'}[p.preparation.phase]||p.preparation.phase)).join('、')||'尚未就绪'))),
          h('details',null,h('summary',null,'高级设置'),h('div',{className:'t-fields'},field('最多执行步骤',form.max_steps,v=>setForm({...form,max_steps:Number(v)}),{type:'number',min:1,max:100})))),
        h('div',{className:'t-card t-wide'},h('h3',null,'工具'),small('按需启用。保存后应用开关；关闭不会删除已有数据。'),
          h('div',{className:'t-tool'},h('div',{className:'t-row t-between'},toggle('apple','日历与提醒事项'),h('span',{className:'t-badge'},state?.config?.tools?.apple===false?'已关闭':state?.apple?.events&&state?.apple?.reminders?'已授权':'待授权')),small('创建、修改与删除仍需在微信中确认。'),form.tools.apple?h('div',null,h('div',{className:'t-actions'},button('授权日历和提醒事项',()=>action('authorize'),state?.running||!state?.configuration||state?.config?.tools?.apple===false)),small(state?.running?'需要重新授权时，先停止 Telos。':'授权对象：Telos Apple')):null),
          h('div',{className:'t-tool'},h('div',{className:'t-row t-between'},toggle('maps','百度地图'),h('span',{className:'t-badge'},state?.config?.tools?.maps===false?'已关闭':credentialState.BAIDU_MAP_AUTH_TOKEN?.configured?'已配置':'待配置')),small('地点、公交与驾车路线、耗时及天气查询。'),form.tools.maps?credential('BAIDU_MAP_AUTH_TOKEN','百度地图 Token'):null))),
      h('div',{className:'t-footer'},h('p',{className:'t-muted'},dirty?'有未保存的修改':state?'配置已保存':'正在读取配置…'),button('保存并应用',()=>action('apply'),!dirty&&state?.configuration,true)),
      state?.busy||notice?h('p',{className:'t-note',role:'status'},state?.busy?'正在应用操作，请稍候…':notice):null,
      state?.error?h('p',{role:'alert',className:'t-note t-error'},state.error):null,
      small('凭据由 DSH 管理，Telos 不保存 Key。关闭 DSH 或禁用插件将停止微信收发。'));
  }
  return {inject:['slots','remote','remote.credentials'],apply(ctx){
    const account={current:null};ctx.inject(['remote.account'],scope=>{account.current=scope.remote.account;scope.effect(()=>()=>{account.current=null;});});
    ctx.slots.inject('plugins.bundle.config',()=>ctx.slots.register({name:'plugins.bundle.config',key:'telos-harness'},()=>h(Setup,{credentials:ctx.remote.credentials,account})));
  }};
}});
