"""safe_untar and lockcheck called in-process (their other tests run them as scripts). Mutation testing (mutmut 3) maps
tests to the functions they call in-process, so these are the tests that can kill its mutants."""
import io
import os
import sys
import tarfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from scripts.monitor.disco_fan_lib import lockcheck, safe_untar  # noqa: E402   (package paths, for mutation testing)

H1, H2, H3 = 'a' * 64, 'b' * 64, 'c' * 64


def tgz(path, members):
    with tarfile.open(path, 'w:gz') as t:
        for name, kind, data in members:
            ti = tarfile.TarInfo(name)
            if kind == 'file':
                ti.size = len(data)
                t.addfile(ti, io.BytesIO(data))
            else:
                ti.type = {'dir': tarfile.DIRTYPE, 'symlink': tarfile.SYMTYPE, 'hardlink': tarfile.LNKTYPE, 'fifo': tarfile.FIFOTYPE}[kind]
                ti.linkname = data.decode() if isinstance(data, bytes) else data
                t.addfile(ti)


def test_untar_files_and_dirs(tmp_path):
    a = tmp_path / 'a.tgz'
    tgz(a, [('d', 'dir', b''), ('d/f.txt', 'file', b'hello'), ('./g.txt', 'file', b'x')])
    safe_untar.main(str(a), str(tmp_path / 'out'))
    assert (tmp_path / 'out' / 'd' / 'f.txt').read_bytes() == b'hello'
    assert (tmp_path / 'out' / 'g.txt').read_bytes() == b'x'
    assert oct((tmp_path / 'out' / 'd' / 'f.txt').stat().st_mode & 0o777) == oct(0o600)


@pytest.mark.parametrize('members', [
    [('/abs', 'file', b'x')], [('../up', 'file', b'x')], [('a/../../up', 'file', b'x')],
    [('l', 'symlink', '/etc/hostname')], [('h', 'hardlink', 'x')], [('p', 'fifo', '')],
    [('.', 'file', b'x')], [('', 'file', b'x')],
])
def test_untar_refuses(tmp_path, members):
    a = tmp_path / 'a.tgz'
    tgz(a, members)
    with pytest.raises(SystemExit):
        safe_untar.main(str(a), str(tmp_path / 'out'))


def test_untar_refuses_existing_dest(tmp_path):
    a = tmp_path / 'a.tgz'
    tgz(a, [('f', 'file', b'x')])
    (tmp_path / 'out').mkdir()
    with pytest.raises(FileExistsError):
        safe_untar.main(str(a), str(tmp_path / 'out'))


def test_untar_refuses_duplicate(tmp_path):
    a = tmp_path / 'a.tgz'
    tgz(a, [('f', 'file', b'one'), ('f', 'file', b'two')])
    with pytest.raises(FileExistsError):
        safe_untar.main(str(a), str(tmp_path / 'out'))
    assert (tmp_path / 'out' / 'f').read_bytes() == b'one'


def test_untar_limits(tmp_path, monkeypatch):
    a = tmp_path / 'a.tgz'
    tgz(a, [('f1', 'file', b'12345'), ('f2', 'file', b'67890')])
    monkeypatch.setattr(safe_untar, 'MAX_BYTES', 9)
    with pytest.raises(SystemExit, match='size limit'):
        safe_untar.main(str(a), str(tmp_path / 'o1'))
    monkeypatch.setattr(safe_untar, 'MAX_BYTES', 10)
    safe_untar.main(str(a), str(tmp_path / 'o2'))
    monkeypatch.setattr(safe_untar, 'MAX_MEMBERS', 1)
    with pytest.raises(SystemExit, match='too many'):
        safe_untar.main(str(a), str(tmp_path / 'o3'))


def write(p, text):
    p.write_text(text)
    return str(p)


def test_lock_norm():
    assert lockcheck.norm('Foo_Bar.baz') == 'foo-bar-baz'
    assert lockcheck.norm('a--b__c') == 'a-b-c'


def test_lock_parse(tmp_path):
    p = write(tmp_path / 'l.txt', f'# c\nFoo_Bar==1.0 \\\n    --hash=sha256:{H1} \\\n    --hash=sha256:{H2}\nbaz==2 ; python_version >= "3.8" --hash=sha256:{H3}  # note\n\n')
    assert lockcheck.parse(p) == {('foo-bar', '1.0'): {H1, H2}, ('baz', '2'): {H3}}


@pytest.mark.parametrize('line', ['foo>=1 --hash=sha256:' + H1, 'foo==1', 'git+https://x/y.git', '-e .', 'foo[extra]==1 --hash=sha256:' + H1,
                                  'foo==1 --hash=sha256:' + 'a' * 63])
def test_lock_parse_refuses(tmp_path, line):
    with pytest.raises(SystemExit):
        lockcheck.parse(write(tmp_path / 'l.txt', line + '\n'))


def test_lock_main(tmp_path, capsys):
    lock = write(tmp_path / 'lock.txt', f'a==1 --hash=sha256:{H1} --hash=sha256:{H2}\nb==2 --hash=sha256:{H3}\n')
    lockcheck.main(lock, write(tmp_path / 't1.txt', f'a==1 --hash=sha256:{H1}\n'))
    assert '1 pins' in capsys.readouterr().out
    with pytest.raises(SystemExit, match='not in the lock'):
        lockcheck.main(lock, write(tmp_path / 't2.txt', f'c==1 --hash=sha256:{H1}\n'))
    with pytest.raises(SystemExit, match='not in the lock'):
        lockcheck.main(lock, write(tmp_path / 't3.txt', f'a==9 --hash=sha256:{H1}\n'))
    with pytest.raises(SystemExit, match='hashes not in the lock'):
        lockcheck.main(lock, write(tmp_path / 't4.txt', f'a==1 --hash=sha256:{H3}\n'))


# --- mutation-testing triage (2026-11-18, e562058): tests for the real survivors ------------------------------------

def test_untar_member_limit_is_exact(tmp_path, monkeypatch):
    a = tmp_path / 'a.tgz'
    tgz(a, [('f1', 'file', b'1'), ('f2', 'file', b'2')])
    monkeypatch.setattr(safe_untar, 'MAX_MEMBERS', 2)
    safe_untar.main(str(a), str(tmp_path / 'ok'))          # exactly the limit passes
    monkeypatch.setattr(safe_untar, 'MAX_MEMBERS', 1)
    with pytest.raises(SystemExit, match=r'^refused archive: too many members$'):
        safe_untar.main(str(a), str(tmp_path / 'no'))


def test_untar_size_limit_message(tmp_path, monkeypatch):
    a = tmp_path / 'a.tgz'
    tgz(a, [('f', 'file', b'123')])
    monkeypatch.setattr(safe_untar, 'MAX_BYTES', 2)
    with pytest.raises(SystemExit, match=r'^refused archive: over the size limit$'):
        safe_untar.main(str(a), str(tmp_path / 'o'))


def test_untar_refusal_names_the_member(tmp_path):
    a = tmp_path / 'a.tgz'
    tgz(a, [('evil', 'symlink', '/etc/hostname')])
    with pytest.raises(SystemExit, match=r"refused archive member: 'evil' type=b'2'"):
        safe_untar.main(str(a), str(tmp_path / 'o'))


def test_untar_dir_member_after_its_files(tmp_path):
    # tar lists a directory after files written into it; the directory member must not fail on the existing dir
    a = tmp_path / 'a.tgz'
    tgz(a, [('d/f', 'file', b'x'), ('d', 'dir', b''), ('d', 'dir', b'')])
    safe_untar.main(str(a), str(tmp_path / 'o'))
    assert (tmp_path / 'o' / 'd' / 'f').read_bytes() == b'x'


def test_lock_target_with_all_lock_hashes_passes(tmp_path, capsys):
    lock = write(tmp_path / 'lock.txt', f'a==1 --hash=sha256:{H1} --hash=sha256:{H2}\n')
    lockcheck.main(lock, write(tmp_path / 't.txt', f'a==1 --hash=sha256:{H1} --hash=sha256:{H2}\n'))   # equal sets: a subset, allowed
    assert '1 pins' in capsys.readouterr().out


def test_lock_parse_refusal_message(tmp_path):
    with pytest.raises(SystemExit, match='unrecognized requirement line in .*: .foo==1.'):
        lockcheck.parse(write(tmp_path / 'l.txt', 'foo==1\n'))
