"""Generate synthetic SILK using the same permissively licensed SDK."""
from pathlib import Path
import subprocess
ROOT=Path(__file__).resolve().parents[1]
def encode_silk(source,target):
    helper=ROOT/'build/native/telos-silk'
    subprocess.run([str(helper),'encode',str(source),str(target)],check=True)
