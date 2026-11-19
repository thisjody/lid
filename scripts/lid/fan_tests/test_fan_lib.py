"""Unit tests for disco_fan's untrusted-archive extractor and wheelhouse lock check (ADR 26-11-06-001).
Run: uv run pytest -q scripts/lid/fan_tests/test_fan_lib.py"""
import io
import os
import subprocess
import sys
import tarfile

import pytest

LIB = os.path.join(os.path.dirname(__file__), "..", "..", "monitor", "disco_fan_lib")


def run(script, *args):
    return subprocess.run([sys.executable, os.path.join(LIB, script), *map(str, args)], capture_output=True, text=True)


def make_tgz(path, members):
    with tarfile.open(path, "w:gz") as t:
        for name, kind, data in members:
            ti = tarfile.TarInfo(name)
            if kind == "file":
                ti.size = len(data)
                t.addfile(ti, io.BytesIO(data))
            elif kind == "dir":
                ti.type = tarfile.DIRTYPE
                t.addfile(ti)
            elif kind == "symlink":
                ti.type, ti.linkname = tarfile.SYMTYPE, data
                t.addfile(ti)
            elif kind == "hardlink":
                ti.type, ti.linkname = tarfile.LNKTYPE, data
                t.addfile(ti)
            elif kind == "fifo":
                ti.type = tarfile.FIFOTYPE
                t.addfile(ti)


def test_regular_files_and_dirs_unpack(tmp_path):
    a = tmp_path / "a.tgz"
    make_tgz(a, [("out", "dir", None), ("out/junit.xml", "file", b"<x/>"), ("out/sub/log.txt", "file", b"hi")])
    r = run("safe_untar.py", a, tmp_path / "dest")
    assert r.returncode == 0, r.stderr
    assert (tmp_path / "dest/out/junit.xml").read_bytes() == b"<x/>"
    assert (tmp_path / "dest/out/sub/log.txt").read_bytes() == b"hi"


@pytest.mark.parametrize("members", [
    [("out/x", "symlink", "/home/someone/.ssh/authorized_keys")],
    [("out/f", "file", b"a"), ("out/h", "hardlink", "out/f")],
    [("../escape.txt", "file", b"x")],
    [("out/../../escape.txt", "file", b"x")],
    [("/abs.txt", "file", b"x")],
    [("out/pipe", "fifo", None)],
])
def test_hostile_members_are_refused(tmp_path, members):
    a = tmp_path / "a.tgz"
    make_tgz(a, members)
    r = run("safe_untar.py", a, tmp_path / "dest")
    assert r.returncode != 0 and "refused" in (r.stderr + r.stdout)
    assert not (tmp_path / "escape.txt").exists() and not (tmp_path.parent / "escape.txt").exists()


def test_never_writes_through_an_existing_path(tmp_path):
    a = tmp_path / "a.tgz"
    make_tgz(a, [("f", "file", b"x")])
    (tmp_path / "dest").mkdir()
    r = run("safe_untar.py", a, tmp_path / "dest")  # the destination must be new
    assert r.returncode != 0


def test_a_duplicate_member_cannot_overwrite(tmp_path):
    a = tmp_path / "a.tgz"
    make_tgz(a, [("out/f", "file", b"first"), ("out/f", "file", b"second")])
    r = run("safe_untar.py", a, tmp_path / "dest")
    assert r.returncode != 0 and (tmp_path / "dest/out/f").read_bytes() == b"first"


H1, H2, H3 = "a" * 64, "b" * 64, "c" * 64


def write(p, text):
    p.write_text(text)
    return p


def test_lockcheck_accepts_a_subset_with_lock_hashes(tmp_path):
    lock = write(tmp_path / "lock.txt", f"pytest==9.2.0 \\\n    --hash=sha256:{H1} \\\n    --hash=sha256:{H2}\nattrs==26.1.0 --hash=sha256:{H3}\n")
    tgt = write(tmp_path / "t.txt", f"pytest==9.2.0 --hash=sha256:{H1}\n")
    r = run("lockcheck.py", lock, tgt)
    assert r.returncode == 0, r.stderr


@pytest.mark.parametrize("target,why", [
    (f"evil==1.0 --hash=sha256:{H1}\n", "not in the lock"),
    (f"pytest==9.2.1 --hash=sha256:{H1}\n", "not in the lock"),
    (f"pytest==9.2.0 --hash=sha256:{H3}\n", "hashes not in the lock"),
    ("pytest==9.2.0\n", "unrecognized"),
    (f"pytest @ https://example.com/pytest.whl --hash=sha256:{H1}\n", "unrecognized"),
    (f"-e ./local --hash=sha256:{H1}\n", "unrecognized"),
])
def test_lockcheck_refuses(tmp_path, target, why):
    lock = write(tmp_path / "lock.txt", f"pytest==9.2.0 --hash=sha256:{H1} --hash=sha256:{H2}\n")
    tgt = write(tmp_path / "t.txt", target)
    r = run("lockcheck.py", lock, tgt)
    assert r.returncode != 0 and why in (r.stderr + r.stdout), r.stderr + r.stdout
