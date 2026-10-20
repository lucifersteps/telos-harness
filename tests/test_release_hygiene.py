"""Privacy and license checks for distributable artifacts."""
import importlib.util
from pathlib import Path
import tarfile

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('release_hygiene', ROOT / 'packaging/release_hygiene.py')
hygiene = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hygiene)


def test_archive_metadata_drops_builder_identity():
    member = tarfile.TarInfo('package/file')
    member.uid, member.gid = 501, 20
    member.uname, member.gname = 'builder', 'staff'
    member.mtime = 12345
    member.pax_headers = {'atime': '12345', 'ctime': '12345', 'path': 'package/file'}
    clean = hygiene.archive_metadata(member)
    assert (clean.uid, clean.gid, clean.uname, clean.gname, clean.mtime) == (0, 0, '', '', 0)
    assert clean.pax_headers == {'path': 'package/file'}


def test_sysconfig_remains_loadable_without_personal_build_paths(tmp_path):
    import runpy
    hygiene.prepare_sysconfig_hook(tmp_path)
    module, = (tmp_path / 'generated').glob('*.py')
    values = runpy.run_path(str(module))['build_time_vars']
    assert values and '/Users/' not in module.read_text()
    assert '/private/var/folders/' not in module.read_text()


def test_source_has_licenses_and_no_gpl_codec_dependency():
    assert 'MIT License' in (ROOT / 'LICENSE').read_text()
    assert not any(line.lower().startswith('pilk==') for line in (ROOT / 'requirements.lock').read_text().splitlines())
    assert 'Skype' in (ROOT / 'vendor/silk/LICENSE').read_text()
    assert 'patent' in (ROOT / 'vendor/silk/LICENSE').read_text().lower()
