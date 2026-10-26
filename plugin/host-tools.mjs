// Reuse the host composition, not a second, hand-maintained tool catalogue.
// Desktop keeps general tools in a preset; CLI normally registers them globally.
const HOST_ONLY=new Set([
  'ask_user_question','enter_plan_mode','exit_plan_mode','present',
  'session_search','session_trace','session_event_trace',
  // User memory discovery is rooted here; changing cwd would change that scope.
  'working_directory',
  // These start or control other conversations. They do not inherit Telos's
  // current-chat history policy, write receipts or WeChat delivery lifecycle.
  'subagent','subagent_fork','list_agents','list_subagent_models',
  'send_message','interrupt_agent','workflow',
  // Native autonomous continuations have no WeChat receipt delivery yet.
  'create_goal','get_goal','update_goal',
]);

export async function mountHostTools(scope,agent) {
  const presets=scope.get('agentPresets');
  if(presets)await presets.mount(scope,'standard');
  // Restrict only channel-incompatible inherited tools, rather than allowing
  // an explicit list. Host additions become available without editing Telos.
  const denied=[...HOST_ONLY].filter(name=>scope.tools.get(name,agent));
  if(denied.length)scope.tools.restrict({deny:denied});
  for(const name of ['tool:goal','tool:session-query'])
    scope.systemPrompt.section({name,order:0,text:'',interpolate:false});
  // Also fail closed if a disabled family is registered later by the host.
  scope.tools.guard(exec=>HOST_ONLY.has(exec.name)?'This tool requires a host conversation; use the current WeChat conversation instead.':undefined);
  scope.on('system-prompt/assemble',async (assembly,context,next)=>{
    assembly.tools=assembly.tools.filter(tool=>!HOST_ONLY.has(tool.name));
    return next();
  });
  return presets;
}
