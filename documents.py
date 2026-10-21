"""Keep received originals; DSH skills and tools decide how to inspect them."""
import hashlib
import os
from pathlib import Path
import tempfile


def document_directory(memory_directory, session):
    return Path(memory_directory) / 'documents' / hashlib.sha256(session.encode()).hexdigest()


async def admit_document(path, name, directory):
    data = path.read_bytes()
    if len(data) > 16 * 1024 * 1024:
        return {'name': name, 'error': '文件超过 16 MiB，请拆分后重发。'}
    suffix = Path(name).suffix.lower()
    if not suffix or len(suffix) > 12 or not suffix[1:].isascii() or not suffix[1:].isalnum():
        suffix = '.bin'
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = directory / (hashlib.sha256(data).hexdigest() + suffix)
    fd, temp = tempfile.mkstemp(dir=directory)
    try:
        with os.fdopen(fd, 'wb') as dest:
            dest.write(data)
        os.replace(temp, target)
    finally:
        Path(temp).unlink(missing_ok=True)
    return {'name': name, 'path': str(target.resolve()), 'size': len(data)}
