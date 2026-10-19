// File loading, change detection and replay belong to DSH, not a Telos memory loop.
import {mkdir,readFile,writeFile,symlink,readlink} from 'node:fs/promises';
import {join} from 'node:path';
import * as AgentInstructions from '@deepseek-ai/dsh-agent-instructions';
import {withFileLock,writeFileAtomic} from '@deepseek-ai/dsh-atomic-write';
import {scopeOf,scopeChainOf} from '@deepseek-ai/dsh-scope';

export const USER_CHAR_LIMIT=1375;
const count=text=>Array.from(text).length;
const snapshot=content=>({content,characters:count(content),limit:USER_CHAR_LIMIT});
async function current(file) {
  try{return await readFile(file,'utf8');}
  catch(error){if(error.code==='ENOENT')return '';throw error;}
}
export async function updateUserFile(file,args,signal) {
  if(args.action==='read')return {ok:true,...snapshot(await current(file))};
  if(args.action!=='write'||typeof args.content!=='string'||typeof args.expected_content!=='string')
    return {ok:false,error:'write requires content and expected_content; read first.'};
  return withFileLock(file,async()=>{
    signal?.throwIfAborted();
    const previous=await current(file);
    if(previous!==args.expected_content)return {ok:false,error:'File changed. Read the current content and merge your edit.',...snapshot(previous)};
    if(count(args.content)>USER_CHAR_LIMIT)return {ok:false,error:'Character limit exceeded. Consolidate relevant entries, then retry. Nothing was written.',proposed_characters:count(args.content),...snapshot(previous)};
    if(args.content===previous)return {ok:true,updated:false,...snapshot(previous)};
    await writeFileAtomic(file,args.content,{mode:0o600,dirMode:0o700});
    return {ok:true,updated:true,...snapshot(args.content)};
  },{signal});
}

export async function mountUserMemory(scope,agent,directory) {
  await scope.plugin({name:'telos-user-memory',inject:['workingDirectory','tools'],async apply(scope) {
  await mkdir(directory,{recursive:true,mode:0o700});
  // A native discovery boundary prevents loading another user's parent workspace.
  for(const name of ['.telos-user','user.md']) {
    try{await writeFile(join(directory,name),'',{flag:'wx',mode:0o600});}
    catch(error){if(error.code!=='EEXIST')throw error;}
  }
  await scope.workingDirectory.set(agent,directory);
  const chain=scopeChainOf(agent);
  const inherited=[...(scope.registry.get(AgentInstructions)?.fibers??[])].find(fiber=>
    fiber.store&&(scopeOf(fiber.ctx)===undefined||chain.includes(scopeOf(fiber.ctx))));
  if(inherited) {
    // CLI already owns this projection. Reuse its file discovery, not a second
    // loader with a conflicting baseline. This is an alias, not a copied profile.
    const candidate=inherited.config.instructionFileCandidates?.[0]??'AGENTS.md';
    if(!candidate||candidate==='.'||candidate==='..'||/[\\/]/.test(candidate)||inherited.config.maxBytes<=0)
      throw new Error('The native instruction loader must enable a local filename and a nonzero budget');
    if(candidate!=='user.md') {
      const alias=join(directory,candidate);
      try{await symlink('user.md',alias);}
      catch(error){if(error.code!=='EEXIST'||await readlink(alias)!=='user.md')throw error;}
    }
  } else await scope.plugin(AgentInstructions,{
    projectRootMarkers:['.telos-user'],instructionFileCandidates:['user.md'],
    localInstructionFileCandidates:[],maxBytes:8192
  });
  const file=join(directory,'user.md');
  scope.tools.register({
    name:'user_memory',
    description:'Maintain this user’s user.md: stable personal facts and standing preferences across conversations. Read before writing; write the complete concise Markdown file with expected_content equal to the exact last read content. Save explicit durable preferences/corrections in the same turn; replace superseded facts and remove facts the user asks to forget (empty content clears the file). Do not save temporary task state, calendar dumps, guesses, credentials, or duplicate facts. Keep all unrelated facts. Limit: 1375 Unicode characters including Markdown and whitespace. On overflow consolidate; on conflict read and merge. Only report saved after ok:true. DSH loads updates automatically; do not repeat the file in chat.',
    parameters:{type:'object',properties:{action:{type:'string',enum:['read','write']},content:{type:'string'},expected_content:{type:'string'}},required:['action'],additionalProperties:false},
    output:{schema:{type:'object',additionalProperties:true},render:(_args,value)=>[{type:'text',text:JSON.stringify(value)}]},
    execute:(args,exec)=>updateUserFile(file,args,exec.signal)
  });
  }});
}
