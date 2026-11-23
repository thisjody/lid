"""The route lock (scripts/lid/route_lock.py), against a model written from Discobox's route rules.

Discobox 0.19 secret routes (`discobox secret create --help`, `secret bind --help`):
- a route is `[METHODS ]HOSTS[PATHS][ graphql=OPS[:FIELDS]][ mcp=TOOLS][=blocked]`; in JSON a route has `hosts` and may carry
  `methods`, `paths`, `graphql`, `mcp`, and `blocked`;
- a host is exact; `*.h` is the hosts beneath h; `*` is any host;
- every list a request is held to (the secret's, then each binding's) must have a route that allows it;
- a `blocked` route only carves out of what other routes allow.
The lock: the credential reaches exactly one host, api.anthropic.com, through the secret AND every binding of that secret.
"""
import os
import sys

import pytest
from hypothesis import given, settings, strategies as st

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from scripts.lid import route_lock  # noqa: E402   (a package path, so mutation testing maps the tests to it)

LOCK = 'api.anthropic.com'


def reachable_hosts(routes):
    """The model: the set of host patterns a route list lets the credential reach. Blocked routes never add a host; extra
    fields (methods, paths, graphql, mcp) only narrow a route within its hosts, so they never add one either."""
    out = set()
    for r in routes or []:
        if r.get('blocked'):
            continue
        out |= set(r.get('hosts') or [])
    return out


def model_holds(secret_routes, bindings, sid):
    """The lock holds iff the secret has an open route, the secret reaches exactly {LOCK} with every open route naming
    only LOCK, and the secret has at least one binding whose list does the same (an empty binding list inherits: refused)."""
    def exact(routes):
        opened = [r for r in routes or [] if not r.get('blocked')]
        return bool(opened) and all(list(dict.fromkeys(r.get('hosts') or [])) == [LOCK] and len(r.get('hosts') or []) == 1 for r in opened)
    mine = [b for b in bindings if b.get('secretId') == sid]
    return exact(secret_routes) and bool(mine) and all(exact(b.get('routes')) for b in mine)


host = st.sampled_from([LOCK, '*.api.anthropic.com', '*.anthropic.com', 'anthropic.com', '*', 'example.com',
                        'api.anthropic.com.evil.test', 'API.ANTHROPIC.COM', 'api.anthropic.com:443', '', '10.0.0.1'])
route = st.fixed_dictionaries(
    {'hosts': st.lists(host, min_size=0, max_size=3)},
    optional={
        'methods': st.lists(st.sampled_from(['GET', 'POST', '*']), max_size=2),
        'paths': st.lists(st.sampled_from(['/v1/**', '/admin/**', '/']), max_size=2),
        'graphql': st.lists(st.just('query'), max_size=1),
        'mcp': st.lists(st.just('*'), max_size=1),
        'blocked': st.booleans(),
    })
routes = st.lists(route, min_size=0, max_size=4)
binding = st.fixed_dictionaries({'id': st.just('b'), 'secretId': st.sampled_from(['s', 'other']), 'routes': routes})


@settings(deadline=None)
@given(routes, st.lists(binding, max_size=4))
def test_lock_matches_model(secret_routes, bindings):
    got = route_lock.lock_problem(secret_routes, bindings, 's') is None
    assert got == model_holds(secret_routes, bindings, 's')


@settings(deadline=None)
@given(routes, st.lists(binding, max_size=4))
def test_a_passing_lock_reaches_only_the_lock_host(secret_routes, bindings):
    # The safety property on its own: whenever the lock passes, the credential can reach api.anthropic.com and nothing else.
    if route_lock.lock_problem(secret_routes, bindings, 's') is None:
        assert reachable_hosts(secret_routes) == {LOCK}
        for b in bindings:
            if b['secretId'] == 's':
                assert reachable_hosts(b['routes']) == {LOCK}


def test_self_test_passes():
    assert route_lock.self_test() == 0


@pytest.mark.parametrize('secret_routes,bindings,why', [
    ([{'hosts': [LOCK, '*.api.anthropic.com']}], [{'secretId': 's', 'routes': [{'hosts': [LOCK]}]}], 'the 0.19 migration shape'),
    ([{'hosts': [LOCK]}], [{'secretId': 's', 'routes': [{'hosts': ['*']}]}], 'the 0.19 migrated binding'),
    ([{'hosts': [LOCK]}], [{'secretId': 's', 'routes': []}], 'an empty binding list inherits'),
    ([{'hosts': [LOCK]}], [{'secretId': 's', 'routes': None}], 'a null binding list'),
    ([{'hosts': [LOCK], 'blocked': True}], [{'secretId': 's', 'routes': [{'hosts': [LOCK]}]}], 'only a blocked route'),
    ([{'hosts': [LOCK.upper()]}], [{'secretId': 's', 'routes': [{'hosts': [LOCK]}]}], 'a host in another case is another string'),
    ([{'hosts': [LOCK + ':443']}], [{'secretId': 's', 'routes': [{'hosts': [LOCK]}]}], 'a host with a port'),
])
def test_refused_shapes(secret_routes, bindings, why):
    assert route_lock.lock_problem(secret_routes, bindings, 's') is not None, why


# --- mutation-testing triage: the messages operators read, and main() in-process --------------------------------------

def test_messages_name_the_list_and_the_hosts():
    p = route_lock.lock_problem([{'hosts': ['example.com']}], [{'id': 'b1', 'secretId': 's', 'routes': [{'hosts': [LOCK]}]}], 's')
    assert p.startswith('secret route is not api.anthropic.com only') and "['example.com']" in p
    p = route_lock.lock_problem([{'hosts': [LOCK]}], [{'id': 'b1', 'secretId': 's', 'routes': [{'hosts': ['*']}]}], 's')
    assert p.startswith('binding b1 route is not') and "['*']" in p
    assert route_lock.lock_problem([{'hosts': [LOCK]}], [], 's') == 'the secret has no binding'
    assert route_lock.route_problem([], 'secret') == 'secret has no routes'
    assert route_lock.route_problem([{'hosts': [LOCK], 'blocked': True}], 'x') == 'x has no open route'


def fake_discobox(monkeypatch, secret, bindings, fail=False, project_wide=None):
    project_wide = project_wide or []
    import json as _json
    import subprocess as _sp

    def co(args, **kw):
        # the real CLI shape, exactly: discobox <command...> -o json
        assert args[:2] == ['discobox', '--auto-start-server=false'] and args[-2:] == ['-o', 'json'], args
        if fail:
            raise _sp.CalledProcessError(1, args)
        if args[2:4] == ['secret', 'get']:
            assert args[4] == 's', args
        if args[2:5] == ['admin', 'harnesses', 'get']:
            assert args[5] == 'claude-code', args
        if args[2:4] == ['secret', 'bound']:
            assert args[4] == '--project-wide', args
        if args[2:4] == ['secret', 'get']:
            return _json.dumps(secret).encode()
        if args[2:5] == ['admin', 'binding', 'ls']:
            return _json.dumps(bindings).encode()
        if args[2:5] == ['admin', 'harnesses', 'get']:
            return _json.dumps({'id': 'h'}).encode()
        if args[2:4] == ['secret', 'bound']:
            return _json.dumps({'boundSecrets': project_wide}).encode()
        raise AssertionError(args)
    monkeypatch.setattr(route_lock.subprocess, 'check_output', co)


@pytest.mark.parametrize('secret,bindings,rc,text', [
    ({'routes': [{'hosts': [LOCK]}]}, {'secretBindings': [{'id': 'b', 'secretId': 's', 'targetKind': 'harnessConfig', 'targetId': 'h', 'routes': [{'hosts': [LOCK]}]}]}, 0, 'route lock ok: s and 1 binding(s)'),
    ({'routes': [{'hosts': ['*']}]}, {'secretBindings': [{'id': 'b', 'secretId': 's', 'targetKind': 'harnessConfig', 'targetId': 'h', 'routes': [{'hosts': [LOCK]}]}]}, 1, 'route lock broken: secret route'),
    ({'routes': [{'hosts': [LOCK]}]}, {'secretBindings': None}, 1, 'route lock broken: the secret has no binding'),
])
def test_main(monkeypatch, capsys, secret, bindings, rc, text):
    fake_discobox(monkeypatch, secret, bindings)
    monkeypatch.setattr(sys, 'argv', ['route_lock.py', 's'])
    assert route_lock.main() == rc
    assert text in capsys.readouterr().out


def test_main_unreadable_is_broken(monkeypatch, capsys):
    fake_discobox(monkeypatch, {}, {}, fail=True)
    monkeypatch.setattr(sys, 'argv', ['route_lock.py', 's'])
    assert route_lock.main() == 1
    assert 'route lock broken: cannot read secret s' in capsys.readouterr().out


def test_main_usage(monkeypatch, capsys):
    monkeypatch.setattr(sys, 'argv', ['route_lock.py'])
    assert route_lock.main() == 2


def test_main_refuses_another_secret_on_the_harness(monkeypatch, capsys):
    fake_discobox(monkeypatch, {'routes': [{'hosts': [LOCK]}]},
                  {'secretBindings': [{'id': 'b', 'secretId': 's', 'targetKind': 'harnessConfig', 'targetId': 'h', 'routes': [{'hosts': [LOCK]}]},
                                      {'id': 'b2', 'secretId': 'old', 'targetKind': 'harnessConfig', 'targetId': 'h', 'routes': [{'hosts': ['*']}]}]})
    monkeypatch.setattr(sys, 'argv', ['route_lock.py', 's'])
    assert route_lock.main() == 1
    assert 'another secret is bound to the claude-code harness: old' in capsys.readouterr().out


def test_main_refuses_project_wide(monkeypatch, capsys):
    fake_discobox(monkeypatch, {'routes': [{'hosts': [LOCK]}]},
                  {'secretBindings': [{'id': 'b', 'secretId': 's', 'targetKind': 'harnessConfig', 'targetId': 'h', 'routes': [{'hosts': [LOCK]}]}]}, project_wide=[{'id': 'sec_pw'}])
    monkeypatch.setattr(sys, 'argv', ['route_lock.py', 's'])
    assert route_lock.main() == 1
    assert 'bound project-wide' in capsys.readouterr().out


def test_main_refuses_an_unread_binding_shape(monkeypatch, capsys):
    fake_discobox(monkeypatch, {'routes': [{'hosts': [LOCK]}]}, {'secretBindings': [{'id': 'b', 'secretId': 's', 'routes': [{'hosts': [LOCK]}]}]})
    monkeypatch.setattr(sys, 'argv', ['route_lock.py', 's'])
    assert route_lock.main() == 1
    assert 'without targetKind/targetId' in capsys.readouterr().out




def test_others_messages_exact():
    h = 'harnessConfig'
    assert route_lock.others_problem([{'id': 'b9', 'secretId': 's'}], 'h', 's', []) == \
        'binding(s) without targetKind/targetId (an unread listing): b9'
    assert route_lock.others_problem([{'id': 'b1', 'targetKind': h}, {'id': 'b2', 'targetId': 'h'}], 'h', 's', []) == \
        'binding(s) without targetKind/targetId (an unread listing): b1, b2'
    assert route_lock.others_problem([], 'h', 's', [{'id': 'sec_a'}, {'secretId': 'sec_b'}]) == 'a secret is bound project-wide: sec_a, sec_b'
    two = [{'id': 'x', 'secretId': 'o1', 'targetKind': h, 'targetId': 'h'}, {'id': 'y', 'secretId': 'o2', 'targetKind': h, 'targetId': 'h'}]
    assert route_lock.others_problem(two, 'h', 's', []) == 'another secret is bound to the claude-code harness: o1, o2'


def test_main_self_test_and_usage(monkeypatch, capsys):
    monkeypatch.setattr(sys, 'argv', ['route_lock.py', '--self-test'])
    assert route_lock.main() == 0
    assert 'route_lock self-test' in capsys.readouterr().out
    monkeypatch.setattr(sys, 'argv', ['route_lock.py', 'a', 'b'])
    assert route_lock.main() == 2
    err = capsys.readouterr().err
    assert err.strip() and err.strip() == route_lock.__doc__.split('\n\n')[1].strip()


def test_others_refuses_other_target_kinds_exact():
    """Y5, through the package path so mutation testing maps it: a binding at any target kind but a harness config is refused,
    named secretId@targetKind, sorted and de-duplicated."""
    mine = {'id': 'b1', 'secretId': 's', 'targetKind': 'harnessConfig', 'targetId': 'h'}
    pool = {'id': 'b2', 'secretId': 'old', 'targetKind': 'pool', 'targetId': 'p1'}
    box = {'id': 'b3', 'secretId': 'z', 'targetKind': 'sandbox', 'targetId': 'x'}
    again = {'id': 'b4', 'secretId': 'old', 'targetKind': 'pool', 'targetId': 'p2'}
    assert route_lock.others_problem([mine, box, pool, again], 'h', 's', []) == \
        'a secret is bound outside a harness config (pool, box, project...): old@pool, z@sandbox'
    assert route_lock.others_problem([mine], 'h', 's', []) is None
