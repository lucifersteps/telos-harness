"""测试专用 Harness 启动器；不进入插件安装包。"""
import asyncio
import os
from pathlib import Path
import shutil
import subprocess
from deepseek_harness_runtime import resolve_bundled_launch_args
from channel import Channel
ROOT=Path(__file__).resolve().parents[1]
def launch_args():
    # Full official Node distribution is required by the upstream SenseVoice worker.
    return [os.environ['TELOS_TEST_NODE'],os.environ['TELOS_TEST_DSH']] if os.environ.get('TELOS_TEST_DSH') else list(resolve_bundled_launch_args())

class Runtime(Channel):
    def __init__(self,root,apple,adapter,*,model='deepseek-flash',api_key,base_url,timeout=180,max_steps=24,event_sink=None):
        from settings import read_config,write_config
        config=read_config(root);config.setdefault('direct_write',False)
        if not (Path(root)/'config.json').exists():config['direct_write']=False
        write_config(root,config)
        super().__init__(root,apple,adapter,timeout=timeout,event_sink=event_sink)
        self.proc=None
        home=self.root/'harness';profiles=home/'profiles'
        if not profiles.exists():shutil.copytree(ROOT/'home'/'profiles',profiles,symlinks=False)
        # The profile installs our channel plugin; SDK JSON-RPC is disabled.
        shutil.copytree(ROOT/'plugin',profiles/'sdk-minimal/node_modules/telos-native-validation',dirs_exist_ok=True)
        package=profiles/'sdk-minimal/node_modules/telos-native-validation'
        (package/'bootstrap.mjs').write_text("export const name='test-business'; export function apply(ctx){ctx.provide('telosBusiness',{url:process.env.TELOS_BUSINESS_URL,token:process.env.TELOS_BUSINESS_TOKEN,skillsDir:process.env.TELOS_SKILLS_DIR,maxSteps:Number(process.env.TELOS_MAX_STEPS||24)});}")
        self.env={**os.environ,'DSH_HOME':str(home),'DSH_AGENTS_HOME':str(home/'agents'),'DEEPSEEK_API_KEY':api_key,'DEEPSEEK_BASE_URL':base_url,
            'TELOS_MODEL':model,'TELOS_BUSINESS_URL':f'http://127.0.0.1:{self.server.server_port}',
            'TELOS_BUSINESS_TOKEN':self.token,'TELOS_MAX_STEPS':str(max_steps),'TELOS_SKILLS_DIR':str(ROOT/'skills')}
        self.command=[*launch_args(),'--profile','sdk-minimal','--patch',str(ROOT/'native.patch.yml')]

    async def start(self):
        if self.proc is None:
            self.log=(self.root/'harness.log').open('a')
            self.proc=subprocess.Popen(self.command,cwd=self.root,env=self.env,stdin=subprocess.DEVNULL,
                stdout=self.log,stderr=self.log)
        await asyncio.wait_for(asyncio.shield(self.ready),15)
        if self.proc.poll() is not None:raise RuntimeError('Harness process exited')

    async def handle(self,*args,**kwargs):
        await self.start()
        return await super().handle(*args,**kwargs)
    async def close(self):
        if self.proc:
            if self.proc.poll() is None:
                self.proc.terminate()
                try:await asyncio.to_thread(self.proc.wait,timeout=10)
                except subprocess.TimeoutExpired:self.proc.kill();await asyncio.to_thread(self.proc.wait)
            self.log.close()
        await super().close()
