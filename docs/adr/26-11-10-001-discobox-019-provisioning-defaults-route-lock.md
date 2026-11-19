# Discobox 0.19: Claude boxes run Discobox's own provisioning defaults; the credential route is the lock

**Status:** Accepted 2026-11-10. Every check in § Verification passed on v0.19.1; the pin moved.
**Date:** 2026-11-10
**Provenance:** Written at decision time.

**Concepts:** box, stack

**Supersedes in part:** the earlier harness-level deny-by-default for Claude boxes (observed 2026-10-30; enforced by `disco_job.sh`'s preflight, `stack_up.sh`, `stack_test.sh`). Everything else in ADRs 26-11-02-001, 26-11-02-002 and 26-11-06-001 stands.

## Context
On v0.18.0 a Claude box booted from a prebuilt harness image, so it needed no network to start. Our lockdown had three parts:
- the WIF secret routed only to `api.anthropic.com`;
- the claude-code harness deny-by-default with an empty allow list;
- connectors off per request (`ENABLE_CLAUDEAI_MCP_SERVERS=false`), with the audit failing a job on any connector traffic.

v0.19 (feat! 0404de82) retired harness images. Every box now applies its bundles at boot and downloads what they need:
- node from `deb.nodesource.com` plus the Debian apt mirrors;
- Claude Code from `registry.npmjs.org`.

With the harness deny-by-default, those downloads are refused, so claude-code never provisions and no job can run. There is no egress field on a create request, and no snapshot path for a libkrun pool. Re-opening only the provisioning hosts would therefore have meant opening them at project, pool or harness level, which reaches every live box anyway.

v0.19 also made the credential route stronger (feat! bef4bc38, 9eaf96f0): a route is exact unless written `*.h`, and a request must fall on a route of *every* list it is held to (the secret's and each binding's).

## Decision
1. **Claude boxes run Discobox's own provisioning defaults.** The claude-code harness carries no egress override of ours (`egress: {}`): whatever its boot provisioning needs is reachable. We don't hand-trim their list.
2. **The lock is the credential route,** narrowed to exactly `api.anthropic.com` on the secret AND on its binding. `disco_job.sh`'s preflight, `stack_up.sh` and `stack_test.sh` assert it on both lists and refuse otherwise. The 0.19 migration writes a host `h` as `[h, *.h]` and a harness binding as `*`, so a freshly migrated install fails this check until it is narrowed.
3. **Credential-free command jobs keep per-box deny-by-default** (`disco_fan.sh`: shell harness, 0 bound secrets, `--egress-deny-by-default`, audit fails on any exchange), for test hermeticity. Its preflight now also asserts that nothing is bound project-wide, because a 0.19 project binding reaches every box.
4. **Unchanged:** connectors off per request; the audit fails a job on connector traffic; no credential inside a box (only a sentinel, swapped at the proxy); WIF, not the subscription login (26-11-02-001).
5. **Driver:** `discobox new --json` became `new --from-json`, which needs `"source"`.

## Why this is safe enough (the reasoning)
- **We are dogfooding Discobox.** Overriding its own provisioning model tests our fork of it, not it.
- **The credential is the thing worth locking.** The token is federated (WIF), short-lived, refreshed by our own timer, and swapped only on a request whose host is exactly `api.anthropic.com` (every list must allow it).
- **Connectors stay off,** and the audit enforces it.
- **It is our own box,** running our own work, and it is off when idle.

What is given up: a Claude box can now reach the open internet while its agent runs. A job's source and output could leave the box. The token cannot.

## Verification
- Route preflight: proven red on `*`, on the migrated `[h, *.h]` shape and on a second host; green when narrowed (lid, 11/10).
- **Decoy proof, `dbx19-decoy3`: PASS.** The box's own sentinel was sent as `Bearer` and as `x-api-key` to a sink on the lid. Both were refused at the proxy, `403 blocked route: this credential has no route to GET <host.internal_ip>/…`. The no-credential control got 204, and the sink logged only `/control`, so nothing was swapped. 0 connector requests; switch `false`. `example.com` 200, open by design.
- **Headless dry run, `dbx19-dry4` (octocat/Hello-World): PASS.** The box provisioned node and Claude Code at boot; `v1/messages` returned 200 through the swap; 0 connector requests; no MCP tools; a patch was exported. 3 of 7 `v1/messages` were not 200: one 400 for an unknown `anthropic-beta` value, which Claude Code dropped and retried; two 502s, both retried.
- **Command jobs:** `dbx19-fan-ok3` (2 shards) PASS, with 0 exchanges after the provisioning cutoff; every pre-cutoff fetch was refused except Discobox's own `*.discobox.internal` store. `dbx19-fan-t2c` FAILS as it must, on the job's own `CONNECT pypi.org:443` after the cutoff (direct rc 7, proxy rc 56, no credential in env).
- **Stack, on the moved pin:** `stack_up` converged (v0.19.1 client and server; route lock on secret and binding); `stack_test` ALL PASS (a)–(i), with the test sandbox now a credential-free shell box created deny-by-default; `stack_check` shows discobox current and no drift.
- **Found and fixed on the way:**
  - `new --json` became `--from-json` + `source`;
  - `audit http --discobox-id` became `--discobox`; `disco_job.sh` had passed a job with an unreadable audit, and now writes a VIOLATION;
  - `disco_fan`'s job user can't be uid 1000, because 0.19's base has `discobox` there;
  - `disco_fan` now waits for boot provisioning to settle and cuts the audit there.

## Consequences
- `stack/stack.yaml`: discobox pin moves to v0.19.1, and the invariants line now reads route lock, not harness deny.
- Rollback: a GCE snapshot of the box's disk (taken with the box off; the 0.19 DB migration is one-way). Delete it after a week of clean runs.
