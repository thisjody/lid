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


def raw_header(name, typeflag, size, size_field=None):
    h = bytearray(512)
    h[0:len(name)] = name
    h[100:108], h[108:116], h[116:124] = b'0000644\0', b'0000000\0', b'0000000\0'
    h[124:136] = size_field if size_field is not None else b'%011o\0' % size
    h[136:148], h[156:157], h[257:263], h[263:265] = b'00000000000\0', typeflag, b'ustar\0', b'00'
    h[148:156] = b' ' * 8
    h[148:156] = b'%06o\0 ' % sum(h)
    return bytes(h)


def raw_tgz(path, blocks):
    import gzip
    with gzip.open(path, 'wb') as g:
        g.write(b''.join(blocks) + bytes(1024))


def long_header(typeflag, text):
    # a GNU long-name or long-link header carrying text (NUL-terminated), padded to whole blocks
    body = text + b'\0'
    return raw_header(b'././@LongLink', typeflag, len(body)) + body + bytes(-len(body) % 512)


def file_blocks(name, data):
    return [raw_header(name, b'0', len(data)), data + bytes(-len(data) % 512)]


@pytest.mark.parametrize('typeflag', [b'x', b'g', b'X'])
def test_untar_refuses_pax_headers(tmp_path, typeflag):
    a = tmp_path / 'a.tgz'
    raw_tgz(a, [raw_header(b'././@PaxHeader', typeflag, 12), b'12 path=abc\n'.ljust(512, b'\0')]
            + file_blocks(b'f.txt', b'x'))
    with pytest.raises(SystemExit, match=f"refused archive: pax extended header type={typeflag!r}"):
        safe_untar.main(str(a), str(tmp_path / 'o'))
    assert not (tmp_path / 'o' / 'f.txt').exists()


@pytest.mark.parametrize('typeflag', [b'L', b'K'])
def test_untar_long_header_cap_is_exact(tmp_path, monkeypatch, typeflag):
    monkeypatch.setattr(safe_untar, 'MAX_XHDR', 600)
    name = b'd/' + b'./' * 298 + b'f'             # 599 bytes, plus the NUL: exactly 600
    ok, big = tmp_path / 'ok.tgz', tmp_path / 'big.tgz'
    raw_tgz(ok, [long_header(typeflag, name)] + file_blocks(b'f.txt', b'x'))
    raw_tgz(big, [long_header(typeflag, name + b'g')] + file_blocks(b'f.txt', b'x'))
    safe_untar.main(str(ok), str(tmp_path / 'o1'))
    with pytest.raises(SystemExit, match='extended header of 601 bytes'):
        safe_untar.main(str(big), str(tmp_path / 'o2'))


def test_untar_refuses_a_negative_long_header_size(tmp_path):
    a = tmp_path / 'a.tgz'
    raw_tgz(a, [raw_header(b'././@LongLink', b'L', 0, size_field=b'\xff' * 12)] + file_blocks(b'f.txt', b'x'))
    with pytest.raises(SystemExit, match='extended header of -1 bytes'):
        safe_untar.main(str(a), str(tmp_path / 'o'))


def test_untar_long_header_chain_is_capped(tmp_path):
    # a long link name then a long name is what tar writes at most; a third in a row is refused
    two, three = tmp_path / 'two.tgz', tmp_path / 'three.tgz'
    raw_tgz(two, [long_header(b'K', b't'), long_header(b'L', b'd/f2.txt')] + file_blocks(b'f', b'x'))
    safe_untar.main(str(two), str(tmp_path / 'o1'))
    assert (tmp_path / 'o1' / 'd' / 'f2.txt').read_bytes() == b'x'
    raw_tgz(three, [long_header(b'L', b'a'), long_header(b'L', b'b'), long_header(b'L', b'c')] + file_blocks(b'f', b'x'))
    with pytest.raises(SystemExit, match='chained extended headers'):
        safe_untar.main(str(three), str(tmp_path / 'o2'))


def test_untar_chain_depth_resets_between_members(tmp_path):
    a = tmp_path / 'a.tgz'
    blocks = []
    for i in range(5):
        blocks += [long_header(b'K', b't'), long_header(b'L', b'f%d.txt' % i)] + file_blocks(b'f', b'x')
    raw_tgz(a, blocks)
    safe_untar.main(str(a), str(tmp_path / 'o'))
    assert sorted(os.listdir(tmp_path / 'o')) == [f'f{i}.txt' for i in range(5)]


def test_untar_refuses_gnu_sparse(tmp_path):
    a = tmp_path / 'a.tgz'
    raw_tgz(a, [raw_header(b'sparse.bin', b'S', 0)])
    with pytest.raises(SystemExit, match=r"refused archive member: 'sparse.bin' type=b'S'"):
        safe_untar.main(str(a), str(tmp_path / 'o'))


def test_untar_refuses_a_negative_size(tmp_path, monkeypatch):
    # base-256 -5000 on a regular file: it would subtract from the byte total, letting later files past the cap.
    # Older tarfile (the lid host's 3.11.2) hands it to the loop, where the guard refuses it; patched releases refuse
    # it themselves. Either way the archive is refused and nothing past the cap is written.
    monkeypatch.setattr(safe_untar, 'MAX_BYTES', 1000)
    a = tmp_path / 'a.tgz'
    raw_tgz(a, [raw_header(b'a.bin', b'0', 0, size_field=b'\xff' * 10 + b'\xec\x78')] + file_blocks(b'b.bin', b'y' * 3000))
    try:
        with tarfile.open(a, 'r:gz') as plain:
            reaches_loop = plain.next().size < 0
    except tarfile.TarError:
        reaches_loop = False
    with pytest.raises((SystemExit, tarfile.TarError)) as e:
        safe_untar.main(str(a), str(tmp_path / 'o'))
    if reaches_loop:
        assert "'a.bin' has a negative size" in str(e.value)
    assert not (tmp_path / 'o' / 'b.bin').exists()


def test_untar_does_not_keep_members(tmp_path):
    # tarfile keeps every member it reads; 2500 dirs with 4 KB long names ('d/././...', which normalises to 'd') would
    # hold about 10 MB
    import tracemalloc
    a = tmp_path / 'a.tgz'
    name = b'd' + b'/.' * 2000
    blocks = []
    for _ in range(2500):
        blocks += [long_header(b'L', name), raw_header(b'd', b'5', 0)]
    raw_tgz(a, blocks)
    tracemalloc.start()
    try:
        safe_untar.main(str(a), str(tmp_path / 'o'))
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert (tmp_path / 'o' / 'd').is_dir()
    assert peak < 3 * 1024 ** 2, peak


def test_untar_long_header_cap_is_path_max():
    assert safe_untar.MAX_XHDR == 4097


def test_untar_refuses_a_corrupt_header_mid_archive(tmp_path):
    # tarfile ends an archive silently at a bad checksum past the first member; that would report a damaged export as whole
    bad = bytearray(raw_header(b'f2.txt', b'0', 1))
    bad[148:156] = b'%06o\0 ' % (int(bad[148:154], 8) + 1)
    a = tmp_path / 'a.tgz'
    raw_tgz(a, file_blocks(b'f1.txt', b'x') + [bytes(bad), b'y'.ljust(512, b'\0')] + file_blocks(b'f3.txt', b'z'))
    with pytest.raises(SystemExit, match='refused archive: bad header'):
        safe_untar.main(str(a), str(tmp_path / 'o'))
    assert not (tmp_path / 'o' / 'f3.txt').exists()


def test_untar_refuses_a_truncated_header_mid_archive(tmp_path):
    import gzip
    a = tmp_path / 'a.tgz'
    with gzip.open(a, 'wb') as g:
        g.write(b''.join(file_blocks(b'f1.txt', b'x')) + raw_header(b'f2.txt', b'0', 1)[:300])
    with pytest.raises(SystemExit, match='refused archive: bad header'):
        safe_untar.main(str(a), str(tmp_path / 'o'))
