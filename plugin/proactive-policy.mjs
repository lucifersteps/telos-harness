// One active policy per scoped Agent. No timers, recipients or delivery access.
const loaders={goal_execution:()=>import('./proactive-policies/goal-execution/index.mjs'),basic:()=>import('./proactive-policies/basic.mjs')};
export function createPolicySlot(options={}) {
  let key=null,module=null,instance=null,bound=null,view=null,generation=0;
  const clear=()=>{instance?.reset();key=null;module=null;instance=null;bound=null;view=null;};
  return {
    reset(){generation++;clear();},
    async bind(event,selection,hasCapability=()=>true) {
      const ticket=++generation;
      if(!event){clear();return;}
      let candidate=null,capabilities={};
      if(selection?.enabled){
        const load=loaders[selection.id];
        if(!load){clear();throw new Error('Unknown proactive policy: '+selection.id);}
        candidate=await load();
        if(ticket!==generation)return;
        capabilities=Object.fromEntries(candidate.dependencies.map(name=>[name,hasCapability(name)]));
      }
      const identity=JSON.stringify([selection,capabilities]);
      if(key!==identity){clear();module=candidate;instance=module?.create(options)??null;key=identity;}
      bound=event;
      instance?.bind(event,selection?.context??{},capabilities);
      view=instance?{id:module.id,enabled:true,dependencies:module.dependencies,
        instructions:module.instructions,context:{...selection.context,...instance.context()}}
        :{id:null,enabled:false,context:{}};
    },
    execute(name,args,exec,run){return instance&&bound?instance.execute(name,args,exec,run):run();},
    beforeNotify(args){instance?.beforeNotify(args);},
    allowsRestrictedRead(name,args){return !!instance?.allowsRestrictedRead(name,args);},
    view(){return view??{id:null,enabled:false,context:{}};}
  };
}
