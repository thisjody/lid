#!/usr/bin/env python3
"""Minimal MCP streamable-HTTP client, stdlib only (runs on the lid host and inside a Discobox sandbox).

  mcp_probe.py [--direct] URL list                 -> tool names, one per line
  mcp_probe.py [--direct] URL call TOOL 'JSONARGS'  -> the tools/call result as JSON
  mcp_probe.py [--direct] URL init                 -> the initialize result as JSON (serverInfo, instructions)

Honors http_proxy/HTTP_PROXY like any urllib client (the sandbox's egress proxy); --direct ignores them.
Exit 0 ok, 1 JSON-RPC error or tool isError, 2 transport failure. Used by stack_up.sh and stack_test.sh (ADR 26-11-03-002).
"""
import json, sys, urllib.request

args = sys.argv[1:]
direct = args[:1] == ['--direct']
if direct:
    args = args[1:]
url, op = args[0], args[1]
opener = urllib.request.build_opener(urllib.request.ProxyHandler({} if direct else None))
session = None


def rpc(method, params=None, rid=None):
    global session
    body = {'jsonrpc': '2.0', 'method': method}
    if params is not None:
        body['params'] = params
    if rid is not None:
        body['id'] = rid
    h = {'Content-Type': 'application/json', 'Accept': 'application/json, text/event-stream'}
    if session:
        h['Mcp-Session-Id'] = session
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=h, method='POST')
    with opener.open(req, timeout=15) as r:
        session = r.headers.get('Mcp-Session-Id') or session
        raw = r.read().decode()
    if rid is None:
        return None
    if raw.lstrip().startswith('{'):
        return json.loads(raw)
    for line in raw.splitlines():  # SSE: the response is the data: line carrying our id
        if line.startswith('data:'):
            msg = json.loads(line[5:])
            if msg.get('id') == rid:
                return msg
    raise RuntimeError('no response for id %s' % rid)


try:
    init = rpc('initialize', {'protocolVersion': '2025-06-18', 'capabilities': {},
                              'clientInfo': {'name': 'epistemic-stack-probe', 'version': '1'}}, 1)
    if 'error' in init:
        print(json.dumps(init['error'])); sys.exit(1)
    if op == 'init':
        print(json.dumps(init.get('result'))); sys.exit(0)
    rpc('notifications/initialized')
    if op == 'list':
        res = rpc('tools/list', {}, 2)
        if 'error' in res:
            print(json.dumps(res['error'])); sys.exit(1)
        for t in res['result'].get('tools', []):
            print(t['name'])
    elif op == 'call':
        res = rpc('tools/call', {'name': args[2], 'arguments': json.loads(args[3] if len(args) > 3 else '{}')}, 3)
        print(json.dumps(res.get('result', res.get('error')))[:2000])
        if 'error' in res or res.get('result', {}).get('isError'):
            sys.exit(1)
    else:
        sys.exit('unknown op %s' % op)
except SystemExit:
    raise
except Exception as e:  # transport failure: refused, proxy 403, timeout
    print('transport: %s: %s' % (type(e).__name__, str(e)[:300]))
    sys.exit(2)
