"""Load stack/stack.yaml with stack/site.yaml merged in (the site's own values; not committed). Used by stack_units.py
and by every script that reads the manifest."""
import os
import re
import sys

import yaml


def _merge(a, b):
    for k, v in b.items():
        if isinstance(v, dict) and isinstance(a.get(k), dict):
            _merge(a[k], v)
        else:
            a[k] = v


# site.yaml may set only these, each to a value of this shape (review finding F12/F13, 2026-11-18): never a pin, digest,
# image, profile or server, and nothing that could break a unit line, a cron line or a shell word.
NAME = r'[a-z]([-a-z0-9]{0,61}[a-z0-9])?'
HOST_KEYS = {
    'name': NAME, 'rebuild_name': NAME, 'project': r'[a-z][-a-z0-9]{4,28}[a-z0-9]', 'zone': r'[a-z]+-[a-z]+[0-9]+-[a-z]',
    'internal_ip': r'(25[0-5]|2[0-4][0-9]|1?[0-9]?[0-9])(\.(25[0-5]|2[0-4][0-9]|1?[0-9]?[0-9])){3}',
    'user': r'[a-z_][a-zA-Z0-9_-]{0,31}', 'commit_name': r"[A-Za-z][A-Za-z .'-]{0,63}",
    'commit_email': r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}',
}
REFUSE_PATTERN = r'[A-Za-z0-9._-]{3,64}'
DISCOBOX_KEYS = {'claude_secret': r'sec_[a-z0-9]{16}'}
POOL_KEYS = {'id': r'pool_[a-z0-9]{16}', 'provider': r'prov_[a-z0-9]{16}'}


def _check(where, value, rx):
    if not isinstance(value, str) or not re.fullmatch(rx, value):
        sys.exit(f'site.yaml {where}: {value!r} is not a valid value')


def _site(s):
    host = s.get('host') or {}
    for k, v in host.items():
        if k == 'refuse_patterns':
            if not isinstance(v, list):
                sys.exit('site.yaml host.refuse_patterns must be a list')
            for x in v:
                _check('host.refuse_patterns[]', x, REFUSE_PATTERN)
        elif k in HOST_KEYS:
            if v is not None:
                _check(f'host.{k}', v, HOST_KEYS[k])
        else:
            sys.exit(f'site.yaml host.{k}: not a site key (allowed: {sorted(HOST_KEYS) + ["refuse_patterns"]})')
    comps = s.get('components') or {}
    for n, v in comps.items():
        if n != 'discobox' or not isinstance(v, dict):
            sys.exit(f'site.yaml components.{n}: only components.discobox (pool ids, claude_secret) is a site key')
        for k, x in v.items():
            if k == 'pool' and isinstance(x, dict):
                for pk, px in x.items():
                    if pk not in POOL_KEYS:
                        sys.exit(f'site.yaml components.discobox.pool.{pk}: not a site key')
                    _check(f'components.discobox.pool.{pk}', px, POOL_KEYS[pk])
            elif k in DISCOBOX_KEYS:
                _check(f'components.discobox.{k}', x, DISCOBOX_KEYS[k])
            else:
                sys.exit(f'site.yaml components.discobox.{k}: not a site key')
    return host, comps


def load(path):
    """The manifest with stack/site.yaml merged in at the same paths. site.yaml holds only the site's own names, address,
    user, commit identity and install ids (validated above); every pin stays in the manifest. A missing site value fails
    where it is read."""
    m = yaml.safe_load(open(path))
    site = os.path.join(os.path.dirname(os.path.abspath(path)), 'site.yaml')
    if os.path.exists(site):
        host, comps = _site(yaml.safe_load(open(site)) or {})
        _merge(m.setdefault('host', {}), host)
        byname = {c['name']: c for c in m['components']}
        for n, v in comps.items():
            _merge(byname[n], v)
    return m


def agw_config(m, cfg_path):
    """stack/agentgateway/config.yaml with the site's address in place of @INTERNAL_IP@."""
    return open(cfg_path).read().replace('@INTERNAL_IP@', str(m['host']['internal_ip']))
