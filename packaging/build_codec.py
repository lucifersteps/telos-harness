"""Build the pinned codec with no developer paths embedded in diagnostics."""
from pathlib import Path
import subprocess
ROOT=Path(__file__).resolve().parents[1]
def build_codec(output=None):
    output=Path(output) if output else ROOT/'build/native/telos-silk'
    output.parent.mkdir(parents=True,exist_ok=True)
    sources=sorted((ROOT/'vendor/silk/src').glob('*.c'))
    subprocess.run(['clang','-O2','-fwrapv','-DNDEBUG','-mmacosx-version-min=14.0',
        '-ffile-prefix-map='+str(ROOT)+'=.', '-fdebug-prefix-map='+str(ROOT)+'=.',
        '-Ivendor/silk/interface','-Ivendor/silk/src','packaging/native/silk.c',
        *[str(p.relative_to(ROOT)) for p in sources],'-o',str(output)],cwd=ROOT,check=True)
    subprocess.run(['strip','-S',str(output)],check=True)
    subprocess.run(['codesign','--force','--sign','-',str(output)],check=True)
    return output
if __name__=='__main__':print(build_codec())
