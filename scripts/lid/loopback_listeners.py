"""Which TCP listeners on the build box could a sandbox reach on host loopback, that are not known to be harmless.

The box user (and so every sandbox, through passt's host-loopback mapping) can reach loopback ports above 1023, apart
from the ToolHive proxies the firewall refuses. A listener counts if it is bound to 127/8, ::1, a v4-mapped loopback, or
a wildcard (0.0.0.0, *, [::], [::ffff:0.0.0.0]), with or without an interface suffix, on a port above 1023. An IPv6
address printed without brackets (old iproute2) cannot be parsed safely and is reported. It is odd unless EVERY process holding the socket is a known owner.
Reads `ss -tlnpH` on stdin; prints the odd listeners; exit 1 if any, 2 if the listing was empty (a failed listing).
TCP only: the host firewall refuses all loopback UDP to the box user except DNS (EPI_STACK_OUTPUT), so UDP needs no list.
Owner names are what the kernel reports and a process can choose its own: this catches configuration drift, it is not a
control against a hostile box user (who already has host loopback).
"""
import re
import sys

KNOWN = re.compile(r'^(discobox[\w.-]*|passt[\w.-]*|thv)$')
# systemd-resolved's LLMNR responder: the host firewall refuses port 5355 to the box user on loopback and the host IP.
ALLOWED_AT = {('systemd-resolve', '5355')}
IFACE = r'(%[^\]\s]+)?'
LOOPBACK = re.compile(r'^(127\.[0-9.]+' + IFACE + r'|0\.0\.0\.0' + IFACE + r'|\*' + IFACE
                      + r'|\[(::1|::|::ffff:127\.[0-9.]+|::ffff:0\.0\.0\.0)' + IFACE + r'\]' + IFACE + r')$')


def odd(text):
    out = []
    for line in text.splitlines():
        f = line.split()
        if len(f) < 5:
            continue
        if ':' in f[3].rpartition(':')[0] and not f[3].startswith('['):
            out.append(f'{f[3]} unbracketed IPv6 (old ss?)')
            continue
        host, _, port = f[3].rpartition(':')
        if not port.isdigit() or int(port) <= 1023 or not LOOPBACK.match(host):
            continue
        owners = re.findall(r'\("([^"]+)",pid=', line)
        if not owners or not all(KNOWN.match(o) or (o, port) in ALLOWED_AT for o in owners):
            out.append(f'{f[3]} {",".join(owners) or "(no owner shown)"}')
    return out


def main():
    text = sys.stdin.read()
    if not text.strip():
        print('the listener listing was empty')
        return 2
    bad = odd(text)
    print('\n'.join(bad))
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
