# The composed stack: what runs now

Source of truth: `stack.yaml`. Why it's this way: ADRs 26-11-06-001 (command jobs fan out across Discobox), 26-11-03-001 (compose by default), 26-11-03-002 (ToolHive), 26-11-03-004 (agentgateway), 26-11-03-003 (box use and idle shutdown), 26-11-02-001/002 (credential and job driver). Last invariant run: `TEST_LOG.md`.

```
desk (laptop) ──lid_run.sh──▶ the build box <host.internal_ip> (host, Debian 12; GCP firewall: IAP ssh only)
  │                              │
  │ stack_up / stack_test        ├─ Discobox v0.19.1 ── Claude job box (claude -p; Discobox's default egress; no credential in the box)
  │                              │     command job box (disco_fan; deny-by-default from creation; no credentials, no agent)
  │ stack_secret (stdin only)    │     sandbox ──HTTP_PROXY──▶ Discobox egress proxy (on the host, uid 1000)
  │ stack_check (gh api, desk)   │        the Claude token is swapped in only on requests to api.anthropic.com (route lock)
  │                              │                 <host.internal_ip> (box-level allow, only with disco_job.sh --tools gateway)
  │                              │
  │                              ├─ host firewall EPI_STACK:
  │                              │     :18800 accepts only from <host.internal_ip>
  │                              │     uid 1000 (Discobox, passt): on <host.internal_ip> only :18800; never 127.0.0.1:18810/18811
  │                              │     epi-agw: only 127.0.0.1:18810/18811, nothing else (no DNS, no internet)
  │                              │
  │                              ├─ agentgateway v1.7.0, system unit epistemic-agw.service, user epi-agw (hardened)
  │                              │     <host.internal_ip>:18800/mcp ── one multiplexed MCP server: time_*, github_* tools
  │                              │     CEL rule: targets time|github only, no write-named tool; access log per call
  │                              │     admin + readiness: unix sockets in /run/epistemic-agw (0750); config read-only, no key
  │                              │            │
  │                              │            ▼ loopback
  │                              └─ ToolHive thv v0.66.0, rootless Podman 5.8.9 (built from upstream; netavark+aardvark-dns, crun 1.31; user socket; no docker.sock)
  │                                    thv-time.service   proxy 127.0.0.1:18810/mcp ── time container (no network)
  │                                    thv-github.service proxy 127.0.0.1:18811/mcp ── github-mcp-server v2.1.0
  │                                          read-only, toolsets context,repos,issues,pull_requests,actions;
  │                                          token injected at container start from the encrypted store;
  │                                          egress via thv's isolated network: *.github.com, *.githubusercontent.com :443
  │                                          (name lookups: thv's dnsmasq as --dns, behind aardvark-dns, which resolves github-egress)
```

## Pieces

| Piece | Where | Started by | Survives reboot by |
|---|---|---|---|
| Podman (rootless) | `/usr/local/bin/podman` v5.8.9, built on the box from the pinned upstream tag + commit (Go pinned by sha256); helpers in `/usr/local/libexec/podman`: netavark (built from source, vendored crates), aardvark-dns, crun (static), all pinned; `~/.config/containers/containers.conf` and `podman.{socket,service}` user units generated; runtime deps from Debian 12 apt | `stack_up.sh` | user unit, linger on |
| thv | `~/.local/bin/thv`, tag + sha256 checked | `stack_up.sh` | binary |
| Secret store | `~/.local/share/toolhive/secrets_encrypted` (ToolHive encrypted provider) | `stack_up.sh` (`thv secret setup`, non-interactive) | file |
| Store password | `~/.config/epistemic-stack/thv_master` (0600, random) → kernel user keyring | `~/stack/bin/thv_prime.sh`, ExecStartPre of every thv unit | re-primed at each unit start |
| MCP servers | `~/.config/systemd/user/thv-<name>.service`, `thv run --foreground`, proxies on 127.0.0.1 | `stack_up.sh` from `stack.yaml` | user units, `default.target` |
| agentgateway | `/usr/local/bin/agentgateway` (tag + sha256 checked), config `/etc/epistemic-stack/agentgateway.yaml` (from `stack/agentgateway/config.yaml`), `epistemic-agw.service` as `epi-agw` | `stack_up.sh` | system unit, `multi-user.target` |
| Gateway session key | `/etc/epistemic-stack/agw.env` (`SESSION_KEY`, 0600 root, random) | `stack_up.sh`, once | file |
| Firewall | `/usr/local/sbin/epistemic_stack_fw.sh`, `epistemic-stack-fw.service` | `stack_up.sh` | system unit |
| Idle shutdown | `/usr/local/sbin/idle_shutdown.sh`, `/etc/cron.d/idle_shutdown`. Busy = ssh, a `job-agent`/`job-*`/`disco-*` unit, a live Discobox box, or load ≥ 0.5. A failed check counts as busy. `--check` prints the reasons | `stack_up.sh` | cron |
| WIF refresh | `wif-refresh.{service,timer}` user units | `stack_up.sh` | user timer |
| Discobox | `~/.local/bin/discobox`, `discobox-server` | by hand 10/24 (not yet scripted) | its own unit; `stack_up` verifies version + lockdown |

## Operating it

- `bash scripts/lid/stack_up.sh`: converge the box to the manifest (box must be ON). Idempotent. A thv pin bump restarts every thv unit, so the proxies run the new binary; the secret store is left as it is.
- `bash scripts/lid/stack_test.sh`: invariants (a)–(i) plus an INFO line on what `initialize` carries, PASS/FAIL, nonzero on FAIL. Run after every stack change and before a job batch.
- `pbpaste | bash scripts/lid/stack_secret.sh github-readonly --expires YYYY-MM-DD`: load or rotate the GitHub token, then up + test. Starts and stops the box itself if it was off.
- `bash scripts/lid/stack_check.sh`: the ▍STACK sweep section (pins vs upstream, advisories since pin, box drift if ON, secret expiry).
- `scripts/monitor/disco_fan.sh <job> <repo> <sha> <cmd> --shards N [--workers W] [--deps req.txt] [--timeout-min M]`: a command job (no agent) fanned out across N boxes (ADR 26-11-06-001).
  - **Boxes:** the `shell` harness (0 secrets), egress denied from creation, the shard as unprivileged `job`, source by `git archive` + `discobox cp`.
  - **Packages:** an optional hash-pinned wheelhouse resolved for the box's Python and glibc.
  - **Capacity:** shards × workers ≤ 2 × pool vCPU (CPU), and shards ≤ `pool.max_boxes` (8, measured: the 9th box does not start). The worker counts live boxes plus its shards against `max_boxes` under the shared box-cap lock.
  - **Audit:** any audit exchange fails the job.
  - **Cleanup:** every box is audited and archived on every path.
  - **Output:** `~/scratch/<job>/logs/run.log` plus `out/summary.json`.
  - **Tests:** `scripts/lid/fan_tests/` (t1–t7, TEST_LOG.md).
- `scripts/monitor/disco_job.sh … --tools gateway`: a job with one MCP server, the gateway, carrying every ToolHive server's tools. Default jobs are unchanged. (`--tools time|github` are retired: the proxies are loopback-only.)

## Known limits

- The store password sits on the same disk as the store (0600, owner only). Encryption at rest protects a copied store file, not a compromised host user. The real boundary is that the token never enters a sandbox.
- No caller authentication yet, at ToolHive or at the gateway. Reach is limited by the firewall and the per-job egress allow. Per-job authorization (ADR 26-11-03-002 step 2, now likely as gateway CEL rules) waits on a per-job identity the sandbox cannot leak.
- **Podman and its helpers are not from apt** (ADR 26-11-03-002, amendment 2026-11-04). Debian 12's Podman 4.3.1 let thv's `--dns` replace aardvark-dns, so the isolated github container could not resolve `github-egress`. Security fixes for podman, netavark, aardvark-dns and crun come only by bumping their pins in `stack.yaml` (`stack_check.sh` flags new releases and advisories). A rebuild downloads Go and Rust toolchains (~1 GB transient, removed after).
- Every gateway job gets github-mcp-server's instructions merged into `initialize` (both targets are open to every job; hide a target per caller with a target `condition`, under which nothing leaks, per ADR 004); they mention write tools the read-only server does not offer.
- `--isolate-network` (github only) pulls `dockurr/dnsmasq:latest` and `ghcr.io/stacklok/toolhive/egress-proxy:latest`. thv pins neither; `stack_check.sh` reports their digests while the box is ON.
