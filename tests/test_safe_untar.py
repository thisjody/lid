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
