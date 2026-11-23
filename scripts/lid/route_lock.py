#!/usr/bin/env python3
"""route_lock.py: the Claude credential's route lock, the one lock ADR 26-11-10-001 keeps (Discobox >= 0.19).

    route_lock.py <secret-id>        exit 0 and print "route lock ok ..." or exit 1 with the reason
    route_lock.py --self-test        the decision function against the shapes that must pass and fail

The lock: the secret's own route list, and the list on every binding of it, is exactly one open route to exactly
api.anthropic.com (blocked carve-outs only narrow). A 0.19 request must fall on a route of EVERY list it is held to,
so each list is checked on its own. A host no longer covers its subdomains (`*.h` does); the 0.19 migration writes h
as [h, *.h] and a harness binding as `*`, so a freshly migrated install fails here until it is narrowed. Policy (ADR 26-11-10-001):
the route is narrowed on the secret AND on the binding, so an empty binding list (which would inherit) fails too.
One copy, used by scripts/monitor/disco_job.sh (shipped into each job), scripts/lid/stack_up.sh and stack_test.sh.
"""
import json
import subprocess
import sys

EXACT = ['api.anthropic.com']


def route_problem(routes, where):
    """None when the list is exactly the lock, else the reason."""
    if not routes:
        return f'{where} has no routes'
    opened = [r for r in routes if not r.get('blocked')]
    if not opened:
        return f'{where} has no open route'
    for r in opened:
        if sorted(r.get('hosts') or []) != EXACT:
            return f'{where} route is not api.anthropic.com only: {r.get("hosts")}'
    return None  # methods/paths/graphql/mcp within the host only narrow it, so they are allowed


def lock_problem(secret_routes, bindings, secret_id):
    """None when the secret and every binding of it hold the lock, else the reason."""
    p = route_problem(secret_routes, 'secret')
    if p:
        return p
    mine = [b for b in bindings if b.get('secretId') == secret_id]
    if not mine:
        return 'the secret has no binding'
    for b in mine:
        p = route_problem(b.get('routes'), f'binding {b.get("id")}')
        if p:
            return p
    return None


def others_problem(bindings, harness_id, secret_id, project_wide):
    """None when no OTHER secret reaches the Claude harness: none bound to the claude-code harness config but ours, and
    nothing bound at any other target kind (pool, box...), and nothing bound project-wide (which reaches every box). An old
    migrated secret left bound would swap for any host."""
    unread = [b.get('id') for b in bindings if 'targetKind' not in b or 'targetId' not in b]
    if unread:
        return 'binding(s) without targetKind/targetId (an unread listing): ' + ', '.join(map(str, unread))
    elsewhere = sorted({f"{b.get('secretId')}@{b.get('targetKind')}" for b in bindings if b.get('targetKind') != 'harnessConfig'})
    if elsewhere:
        return 'a secret is bound outside a harness config (pool, box, project...): ' + ', '.join(elsewhere)
    if project_wide:
        return 'a secret is bound project-wide: ' + ', '.join(str(b.get('id') or b.get('secretId') or b) for b in project_wide)
    others = sorted({b.get('secretId') for b in bindings
                     if b.get('targetKind') == 'harnessConfig' and b.get('targetId') == harness_id and b.get('secretId') != secret_id})
    if others:
        return 'another secret is bound to the claude-code harness: ' + ', '.join(map(str, others))
    return None


def self_test():
    ok = [{'hosts': ['api.anthropic.com']}]
    b = lambda r, s='s': {'id': 'b', 'secretId': s, 'routes': r}
    cases = [  # (secret routes, bindings, must pass)
        (ok, [b(ok)], True),
        (ok + [{'hosts': ['api.anthropic.com'], 'paths': ['/admin/**'], 'blocked': True}], [b(ok)], True),
        (ok, [b(ok), b(ok, 'other')], True),
        (ok, [b(ok), b([{'hosts': ['*']}], 'other')], True),  # another secret's binding is not this lock
        ([{'hosts': ['*.api.anthropic.com', 'api.anthropic.com']}], [b(ok)], False),  # the 0.19 migrated shape
        (ok, [b([{'hosts': ['*']}])], False),  # the 0.19 migrated binding
        (ok, [b([])], False),  # an empty binding list inherits: refused (narrowed on both, ADR 26-11-10-001)
        (ok + [{'hosts': ['example.com']}], [b(ok)], False),
        ([], [b(ok)], False),
        (ok, [], False),
        ([{'hosts': ['api.anthropic.com'], 'blocked': True}], [b(ok)], False),
    ]
    bad = [i for i, (s, bs, want) in enumerate(cases) if (lock_problem(s, bs, 's') is None) != want]
    print(f'route_lock self-test: {len(cases) - len(bad)}/{len(cases)} ' + ('ok' if not bad else f'FAILED cases {bad}'))
    return 1 if bad else 0


def main():
    if sys.argv[1:] == ['--self-test']:
        return self_test()
    if len(sys.argv) != 2:
        print(__doc__.split('\n\n')[1], file=sys.stderr)
        return 2
    sec = sys.argv[1]
    get = lambda *a: json.loads(subprocess.check_output(['discobox', '--auto-start-server=false', *a, '-o', 'json']))
    try:
        secret_routes = get('secret', 'get', sec).get('routes')
        bindings = get('admin', 'binding', 'ls')['secretBindings'] or []
        harness_id = get('admin', 'harnesses', 'get', 'claude-code')['id']
        project_wide = get('secret', 'bound', '--project-wide')['boundSecrets'] or []
    except (subprocess.CalledProcessError, ValueError, KeyError) as e:
        print(f'route lock broken: cannot read secret {sec} or its bindings ({e.__class__.__name__})')
        return 1
    p = lock_problem(secret_routes, bindings, sec) or others_problem(bindings, harness_id, sec, project_wide)
    if p:
        print(f'route lock broken: {p}')
        return 1
    n = sum(1 for x in bindings if x.get('secretId') == sec)
    print(f'route lock ok: {sec} and {n} binding(s) route only to api.anthropic.com')
    return 0


if __name__ == '__main__':
    sys.exit(main())
