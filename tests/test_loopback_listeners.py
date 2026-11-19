"""The loopback-listener check (scripts/lid/loopback_listeners.py), stack_test (j). A package import at module level, so
mutation testing maps these tests to it."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from scripts.lid import loopback_listeners as ll  # noqa: E402


SS = '''LISTEN 0 4096 127.0.0.1:33225 0.0.0.0:* users:(("discobox-server",pid=10,fd=3))
LISTEN 0 4096 127.0.0.1:18810 0.0.0.0:* users:(("thv",pid=11,fd=7))
LISTEN 0 128 0.0.0.0:22 0.0.0.0:* users:(("sshd",pid=1,fd=3))
LISTEN 0 4096 0.0.0.53%lo:53 0.0.0.0:* users:(("systemd-resolve",pid=5,fd=14))
LISTEN 0 4096 10.0.0.5:18800 0.0.0.0:* users:(("agentgateway",pid=9,fd=9))
'''


# the parse is a tested helper; a listener at 127/8, ::1, a wildcard or a v4-mapped loopback, above 1023, owned by
# anything but the known owners, is reported; a shared socket fails if ANY owner is unknown.
def test_loopback_listeners_flags_the_right_ones():
    assert ll.odd(SS) == []
    for line in ('LISTEN 0 5 0.0.0.0:8000 0.0.0.0:* users:(("python3",pid=2,fd=3))',
                 'LISTEN 0 5 *:8000 *:* users:(("node",pid=2,fd=3))',
                 'LISTEN 0 5 [::]:8000 [::]:* users:(("node",pid=2,fd=3))',
                 'LISTEN 0 5 [::1]:9000 [::]:* users:(("x",pid=2,fd=3))',
                 'LISTEN 0 5 [::ffff:127.0.0.1]:9000 [::]:* users:(("x",pid=2,fd=3))',
                 'LISTEN 0 5 127.0.0.1:9000 0.0.0.0:* users:(("discobox-server",pid=10,fd=3),("leak",pid=12,fd=3))',
                 'LISTEN 0 5 127.0.0.1:9000 0.0.0.0:*'):
        assert ll.odd(SS + line + '\n'), line
    assert ll.odd('LISTEN 0 5 10.0.0.5:9000 0.0.0.0:* users:(("x",pid=2,fd=3))\n') == []


# systemd-resolved's LLMNR listener (5355) is allowed, by owner AND port only; the firewall refuses 5355 to the box user.
def test_loopback_listeners_resolved_llmnr_only():
    assert ll.odd('LISTEN 0 4096 0.0.0.0:5355 0.0.0.0:* users:(("systemd-resolve",pid=5,fd=11))\n') == []
    assert ll.odd('LISTEN 0 4096 [::]:5355 [::]:* users:(("systemd-resolve",pid=5,fd=13))\n') == []
    assert ll.odd('LISTEN 0 4096 0.0.0.0:5356 0.0.0.0:* users:(("systemd-resolve",pid=5,fd=11))\n')
    assert ll.odd('LISTEN 0 4096 0.0.0.0:5355 0.0.0.0:* users:(("python3",pid=5,fd=11))\n')


def test_main_exit_codes(monkeypatch, capsys):
    import io
    monkeypatch.setattr(sys, 'stdin', io.StringIO(''))
    assert ll.main() == 2 and 'empty' in capsys.readouterr().out
    monkeypatch.setattr(sys, 'stdin', io.StringIO('LISTEN 0 5 127.0.0.1:9000 0.0.0.0:* users:(("x",pid=2,fd=3))\n'))
    assert ll.main() == 1 and capsys.readouterr().out.strip() == '127.0.0.1:9000 x'
    monkeypatch.setattr(sys, 'stdin', io.StringIO('LISTEN 0 5 127.0.0.1:9000 0.0.0.0:* users:(("thv",pid=2,fd=3))\n'))
    assert ll.main() == 0


def test_edges():
    assert ll.odd('LISTEN 0 5 127.0.0.1:1023 0.0.0.0:* users:(("x",pid=2,fd=3))\n') == []
    assert ll.odd('LISTEN 0 5 127.0.0.1:1024 0.0.0.0:* users:(("x",pid=2,fd=3))\n') == ['127.0.0.1:1024 x']
    assert ll.odd('LISTEN 0 5 127.0.0.53%lo:5353 0.0.0.0:* users:(("x",pid=2,fd=3))\n') == ['127.0.0.53%lo:5353 x']
    assert ll.odd('LISTEN 0 5 127.0.0.1:9000 0.0.0.0:*\n') == ['127.0.0.1:9000 (no owner shown)']
    assert ll.odd('LISTEN 0 5 127.0.0.1:9000 0.0.0.0:* users:(("a",pid=2,fd=3),("b",pid=3,fd=3))\n') == ['127.0.0.1:9000 a,b']
    assert ll.odd('garbage\n\nLISTEN 0 5 127.0.0.1:x 0.0.0.0:* users:(("x",pid=2,fd=3))\n') == []
    assert ll.odd('LISTEN 0 5 127.0.0.1:9000 0.0.0.0:* users:(("discobox-server",pid=2,fd=3))\n') == []
    assert ll.odd('LISTEN 0 5 127.0.0.1:9000 0.0.0.0:* users:(("xdiscobox",pid=2,fd=3))\n')
    assert ll.odd('LISTEN 0 5 127.0.0.1:9000 0.0.0.0:* users:(("thvx",pid=2,fd=3))\n')


def test_a_short_line_is_skipped_not_the_end():
    assert ll.odd('garbage\nLISTEN 0 5 127.0.0.1:9000 0.0.0.0:* users:(("x",pid=2,fd=3))\n') == ['127.0.0.1:9000 x']


def test_main_output_exact(monkeypatch, capsys):
    import io
    monkeypatch.setattr(sys, 'stdin', io.StringIO('\n'))
    assert ll.main() == 2 and capsys.readouterr().out == 'the listener listing was empty\n'
    two = ('LISTEN 0 5 127.0.0.1:9000 0.0.0.0:* users:(("x",pid=2,fd=3))\n'
           'LISTEN 0 5 127.0.0.1:9001 0.0.0.0:* users:(("y",pid=3,fd=3))\n')
    monkeypatch.setattr(sys, 'stdin', io.StringIO(two))
    assert ll.main() == 1 and capsys.readouterr().out == '127.0.0.1:9000 x\n127.0.0.1:9001 y\n'


# An interface suffix on any form, and the v4-mapped wildcard, still count.
def test_iface_suffix_and_mapped_wildcard_count():
    for addr in ('0.0.0.0%lo', '*%lo', '[::]%lo', '[::1]%lo', '[::ffff:127.0.0.1]%lo', '[::ffff:0.0.0.0]', '[::ffff:0.0.0.0]%lo',
                 '127.0.0.1%lo', '[::1%lo]'):
        line = f'LISTEN 0 5 {addr}:9001 0.0.0.0:* users:(("x",pid=2,fd=3))\n'
        assert ll.odd(line) == [f'{addr}:9001 x'], addr
    assert ll.odd('LISTEN 0 5 10.0.0.5%ens4:9001 0.0.0.0:* users:(("x",pid=2,fd=3))\n') == []


# Any interface-name characters, and an unbracketed IPv6 address fails closed.
def test_any_iface_chars_and_unbracketed_v6_fail_closed():
    for addr in ('0.0.0.0%br+1', '[::1%veth@if3]', '127.0.0.1%a#b'):
        assert ll.odd(f'LISTEN 0 5 {addr}:9001 0.0.0.0:* users:(("x",pid=2,fd=3))\n') == [f'{addr}:9001 x'], addr
    for addr in (':::9001', '::1:9001', '::ffff:127.0.0.1:9001', 'fe80::1:9001'):
        assert ll.odd(f'LISTEN 0 5 {addr} :::* users:(("thv",pid=2,fd=3))\n') == [f'{addr} unbracketed IPv6 (old ss?)'], addr


def test_unbracketed_check_reads_the_local_field_and_keeps_going():
    assert ll.odd('LISTEN 0 5 10.0.0.5:9001 :::* users:(("x",pid=2,fd=3))\n') == []
    two = ('LISTEN 0 5 :::9001 *:* users:(("thv",pid=2,fd=3))\n'
           'LISTEN 0 5 127.0.0.1:9002 0.0.0.0:* users:(("x",pid=3,fd=3))\n')
    assert ll.odd(two) == [':::9001 unbracketed IPv6 (old ss?)', '127.0.0.1:9002 x']
