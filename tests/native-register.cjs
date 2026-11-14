// Run isolated JS tests against the exact installed Desktop runtime.
const {registerHooks,createRequire}=require('node:module');
const {pathToFileURL}=require('node:url');
const root=process.env.DSH_NATIVE_ROOT;
if(!root)throw new Error('DSH_NATIVE_ROOT must point to the host @deepseek-ai directory');
const native=createRequire(root+'/dsh-session/lib/index.js');
registerHooks({resolve(specifier,context,next){
  if(specifier.startsWith('@deepseek-ai/')&&!context.parentURL?.includes('/app.asar/'))
    return next(pathToFileURL(native.resolve(specifier)).href,context);
  return next(specifier,context);
}});
