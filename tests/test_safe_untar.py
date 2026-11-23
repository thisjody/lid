"""safe_untar.py (scripts/monitor/disco_fan_lib/), against generated hostile archives.

The promise, from its docstring: only regular files and directories, relative paths, no "..", no absolute paths, no
symlinks, hard links, devices or FIFOs; nothing is ever written through an existing path; the destination is new; it exits
non-zero on the first refused member. The property: whatever the archive, after the run every path under the destination
is a plain file or directory whose real path is inside the destination, and nothing outside the destination changed.
"""
import io
import os
import subprocess
import sys
import tarfile

import pytest

from hypothesis import given, settings, strategies as st

LIB = os.path.join(os.path.dirname(__file__), '..', 'scripts', 'monitor', 'disco_fan_lib', 'safe_untar.py')

name_part = st.sampled_from(['a', 'b', 'c', '..', '.', '', 'x' * 40, 'ok.txt', 'evil'])
name = st.lists(name_part, min_size=1, max_size=4).map('/'.join) | st.sampled_from(['/etc/passwd', '/tmp/x', '../up', 'a/../../up'])
kind = st.sampled_from(['file', 'dir', 'symlink', 'hardlink', 'fifo', 'chr', 'blk'])
member = st.tuples(name, kind, st.binary(max_size=64), st.sampled_from(['a', '/etc/hostname', '../../outside', 'ok.txt']))


def build(path, members):
    with tarfile.open(path, 'w:gz') as t:
        for n, k, data, target in members:
            ti = tarfile.TarInfo(n or '.')
            if k == 'file':
                ti.size = len(data)
                t.addfile(ti, io.BytesIO(data))
                continue
            ti.type = {'dir': tarfile.DIRTYPE, 'symlink': tarfile.SYMTYPE, 'hardlink': tarfile.LNKTYPE,
                       'fifo': tarfile.FIFOTYPE, 'chr': tarfile.CHRTYPE, 'blk': tarfile.BLKTYPE}[k]
            ti.linkname = target
            t.addfile(ti)


def snapshot(root):
    out = {}
    for d, dirs, files in os.walk(root):
        for f in files + dirs:
            p = os.path.join(d, f)
            st_ = os.lstat(p)
            out[p] = (st_.st_mode, st_.st_size, st_.st_mtime_ns)
    return out


@settings(deadline=None)
@given(st.lists(member, min_size=1, max_size=6))
def test_never_escapes_or_creates_special_files(tmp_path_factory, members):
    base = tmp_path_factory.mktemp('u')
    outside = base / 'outside'
    outside.mkdir()
    (outside / 'keep').write_text('keep')
    arc = base / 'a.tgz'
    build(arc, members)
    before = snapshot(outside)
    dest = base / 'work' / 'dest'
    (base / 'work').mkdir()
    r = subprocess.run([sys.executable, LIB, str(arc), str(dest)], capture_output=True, text=True)
    real_dest = os.path.realpath(dest)
    for d, dirs, files in os.walk(dest):
        for f in files + dirs:
            p = os.path.join(d, f)
            m = os.lstat(p).st_mode
            assert os.path.realpath(p).startswith(real_dest + os.sep), p
            assert os.path.isfile(p) or os.path.isdir(p), p
            assert not os.path.islink(p), p
    assert snapshot(outside) == before
    bad = [m for m in members if m[1] not in ('file', 'dir')]
    if bad:
        # a refused member type anywhere means the run fails (possibly after writing earlier members)
        assert r.returncode != 0


def test_existing_destination_is_refused(tmp_path):
    arc = tmp_path / 'a.tgz'
    build(arc, [('ok.txt', 'file', b'x', 'a')])
    dest = tmp_path / 'dest'
    dest.mkdir()
    r = subprocess.run([sys.executable, LIB, str(arc), str(dest)], capture_output=True, text=True)
    assert r.returncode != 0


# Extended headers (security scan F1 and its two blind reviews, 2026-11-20). tarfile parses extended headers before the
# unpacker's loop sees a member, and on 3.11 its pax parser can be driven to GBs of memory or minutes of CPU by one small
# header. The export is GNU tar's default format (GNU long-name headers, never pax), so pax is refused before tarfile
# parses it, and long-name headers are capped in size and in chain length.

def run_untar(arc, dest):
    return subprocess.run([sys.executable, LIB, str(arc), str(dest)], capture_output=True, text=True, timeout=60)


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


@pytest.mark.parametrize('typeflag', [b'x', b'g', b'X'])
def test_pax_headers_are_refused_before_parsing(tmp_path, typeflag):
    # 3.11's pax parser is quadratic on a header of digits; refused on its type, before any of it is read
    arc = tmp_path / 'a.tgz'
    raw_tgz(arc, [raw_header(b'././@PaxHeader', typeflag, 65536), b'1' * 65536,
                  raw_header(b'f.txt', b'0', 1), b'x'.ljust(512, b'\0')])
    r = run_untar(arc, tmp_path / 'dest')
    assert r.returncode != 0
    assert 'pax extended header' in r.stderr
    assert not (tmp_path / 'dest' / 'f.txt').exists()


def test_pax_format_archive_is_refused(tmp_path):
    arc = tmp_path / 'a.tgz'
    with tarfile.open(arc, 'w:gz', format=tarfile.PAX_FORMAT) as t:
        ti = tarfile.TarInfo('a/' + 'b' * 150 + '.txt')
        ti.size = 2
        t.addfile(ti, io.BytesIO(b'ok'))
    r = run_untar(arc, tmp_path / 'dest')
    assert r.returncode != 0
    assert 'pax extended header' in r.stderr


def test_gnu_long_names_still_unpack(tmp_path):
    # what GNU tar's default format writes for paths over 100 characters
    long_name = 'a/' + 'b' * 150 + '/' + 'c' * 150 + '.txt'
    arc = tmp_path / 'a.tgz'
    with tarfile.open(arc, 'w:gz', format=tarfile.GNU_FORMAT) as t:
        ti = tarfile.TarInfo(long_name)
        ti.size = 2
        t.addfile(ti, io.BytesIO(b'ok'))
    dest = tmp_path / 'dest'
    r = run_untar(arc, dest)
    assert r.returncode == 0, r.stderr
    assert (dest / long_name).read_bytes() == b'ok'


def test_oversized_gnu_longname_is_refused(tmp_path):
    arc = tmp_path / 'a.tgz'
    with tarfile.open(arc, 'w:gz', format=tarfile.GNU_FORMAT) as t:
        ti = tarfile.TarInfo('d/' + 'n' * (200 * 1024))
        ti.size = 1
        t.addfile(ti, io.BytesIO(b'x'))
    r = run_untar(arc, tmp_path / 'dest')
    assert r.returncode != 0
    assert 'extended header of' in r.stderr


def test_declared_huge_longname_is_refused_before_reading(tmp_path):
    # the header declares 64 MiB and the archive ends right after it: refused on the declared size, not on a short read
    arc = tmp_path / 'a.tgz'
    raw_tgz(arc, [raw_header(b'././@LongLink', b'L', 64 << 20)])
    r = run_untar(arc, tmp_path / 'dest')
    assert r.returncode != 0
    assert 'extended header of 67108864 bytes' in r.stderr


def test_gnu_sparse_member_is_refused(tmp_path):
    h = bytearray(raw_header(b'sparse.bin', b'S', 0))
    h[482] = 1                                      # isextended: sparse extension blocks follow
    h[148:156] = b' ' * 8
    h[148:156] = b'%06o\0 ' % sum(h)
    ext = bytearray(512)
    ext[504] = 1                                    # each extension block says another follows
    arc = tmp_path / 'a.tgz'
    raw_tgz(arc, [bytes(h)] + [bytes(ext)] * 20000)
    r = run_untar(arc, tmp_path / 'dest')
    assert r.returncode != 0
    assert "type=b'S'" in r.stderr


def test_truncated_archive_is_a_clean_refusal(tmp_path):
    arc = tmp_path / 'a.tgz'
    import gzip
    with gzip.open(arc, 'wb') as g:
        g.write(raw_header(b'big', b'0', 1 << 30))
    r = run_untar(arc, tmp_path / 'dest')
    assert r.returncode != 0
    assert 'refused archive' in r.stderr
    assert 'Traceback' not in r.stderr


def test_duplicate_file_is_a_clean_refusal(tmp_path):
    arc = tmp_path / 'a.tgz'
    raw_tgz(arc, [raw_header(b'f.txt', b'0', 1), b'x'.ljust(512, b'\0'), raw_header(b'f.txt', b'0', 1), b'y'.ljust(512, b'\0')])
    r = run_untar(arc, tmp_path / 'dest')
    assert r.returncode != 0
    assert 'refused archive: FileExistsError' in r.stderr
    assert 'Traceback' not in r.stderr
