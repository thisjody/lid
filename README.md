# lid

A build box for unattended coding agents, composed from open source and pinned in one manifest. Agent jobs run in throwaway [Discobox](https://github.com/discobox-ai/discobox) sandboxes with **no credential inside the box**. The Claude token is swapped in at Discobox's proxy, and only on requests to `api.anthropic.com`. Tools are reached through one gateway that refuses writes. Every outbound request a box makes is audited, and the box powers itself off when idle.

| Layer | Upstream | Role here |
|---|---|---|
| Sandboxes | [Discobox](https://github.com/discobox-ai/discobox) | One box per job. Secrets are held by Discobox's proxy and swapped in on the way out; the box sees only a sentinel. The Claude credential is route-locked to `api.anthropic.com` on the secret and on its binding. |
| Tools | [ToolHive](https://github.com/stacklok/toolhive) | MCP servers in their own containers, holding the tool credentials. Jobs hold none. |
| Gateway | [agentgateway](https://github.com/agentgateway/agentgateway) | One MCP endpoint for jobs, with CEL tool authorization (write tools refused) and a per-call access log. |
| Runtime | [Podman](https://github.com/podman-container-tools/podman) | Rootless, built from upstream. |

The Claude credential comes from workload identity federation, so no static key exists anywhere (ADR 26-11-02-001).

## What it proves, and how
- `scripts/lid/stack_test.sh` checks the invariants from inside a locked sandbox: no credential in the sandbox's env or filesystem; no internet; ToolHive reachable only through the gateway; write tools absent and refused; the gateway's own egress confined; the audit clean.
- `scripts/lid/decoy_proof.sh` has a box send its own sentinel to a sink on the host (as `Bearer`, as `x-api-key` and in a JSON body). It passes only if the real token never arrives.
- `scripts/lid/route_lock.py --self-test` covers the route lock's cases.

What has been tested, how, and what hasn't, is in `VERIFICATION.md`. The checks ran on the pre-publication commits; this
commit differs from them only in comments, docs and test selection.

## Layout
- `stack/stack.yaml`: the manifest. Every component is pinned by tag, commit and sha256, with its invariants.
- `stack/site.example.yaml`: the site's own values (box name, GCP project and zone, address, user, commit identity). Copy it to `stack/site.yaml`, which is not committed.
- `stack/DESIGN.md`: what runs now. `stack/GAPS.md`: known gaps. `stack/TEST_LOG.md`: test runs.
- `scripts/lid/`:
  - `stack_up.sh` converges the box to the manifest, from a blank Debian 12 VM;
  - `discobox_up.sh` (step 0 of `stack_up`) installs and configures Discobox;
  - `stack_test.sh`, `decoy_proof.sh`, `route_lock.py`: the proofs above;
  - `stack_check.sh` compares the pins with upstream releases and advisories, and the box with the manifest;
  - `stack_secret.sh` loads the GitHub token into ToolHive's encrypted store from stdin;
  - `idle_shutdown.sh` powers the box off after 20 minutes idle.
- `scripts/monitor/disco_job.sh`: one headless Claude Code job in a box. `scripts/monitor/disco_fan.sh` + `disco_fan_lib/`: command jobs (test suites, mutation runs) fanned out across deny-by-default boxes.
- `scripts/lid_run.sh`: the only path from the operator's machine ("the desk") to the box, over IAP ssh. It checks the remote hostname before running anything.
- `docs/adr/`: the decisions and their reasons.

## Capacity (measured, see `VERIFICATION.md`)
- On the default pool (6 vCPU, 20 GiB, an n1-standard-8 host) at most **8 boxes run at once**: the 9th dies before its
  sandbox agent is healthy. The manifest records this as `pool.max_boxes`, and every box creator counts against it
  under one lock.
- **Claude jobs:** up to 6 in parallel; a 7th is refused at preflight. A backlog of 12 drains 6 at a time with no
  failure.
- **Fan-out jobs:** up to 8 shards (the box ceiling) and `shards x workers` up to 2 x the pool's vCPUs.
- Box creation took p50 12 s and p99 26 s with 6 at once (24 s and 41 s in an earlier run); host memory peaked at 7.4
  to 11.2 of 30 GB.

## Standing one up
**Requirements.** lid is built for one setup and tested only on it: a Google Compute Engine VM reached over IAP ssh, an
Anthropic workload identity federation rule for the Claude credential, and the Discobox, ToolHive and agentgateway
versions pinned in `stack/stack.yaml`. Other clouds or credential schemes would need changes.

1. **A GCE VM:** Debian 12, n1-standard-8, `--enable-nested-virtualization`, no external address (IAP ssh only), and a service account of its own.
2. **An Anthropic workload identity federation rule** that trusts that service account. Put its four ids in `~/.config/anthropic_wif.env` on the box.
3. **`stack/site.yaml`,** from the example.
4. **Converge, then load the token and test:**
   ```
   bash scripts/lid/stack_up.sh
   pbpaste | bash scripts/lid/stack_secret.sh github-readonly --expires YYYY-MM-DD   # a read-only GitHub token
   bash scripts/lid/stack_test.sh
   bash scripts/lid/decoy_proof.sh
   ```
   `stack_up` prints the Discobox install ids on its first run; record them in `site.yaml`.

## Decisions

| ADR | Decision |
|---|---|
| [26-11-02-001](docs/adr/26-11-02-001-build-box-claude-credential-by-workload-identity.md) | Claude credential by workload identity federation; no static key exists |
| [26-11-02-002](docs/adr/26-11-02-002-box-jobs-headless-parallel-principal-authored.md) | Jobs run the agent headless, up to six in parallel, with commits authored as the box owner |
| [26-11-03-001](docs/adr/26-11-03-001-compose-by-default-pinned-stack-manifest.md) | Compose by default: assembled from upstream open source under a pinned manifest |
| [26-11-03-002](docs/adr/26-11-03-002-toolhive-tool-layer-behind-discobox.md) | ToolHive is the tool layer; credentials stay outside the sandbox |
| [26-11-03-003](docs/adr/26-11-03-003-heavy-work-on-the-box-and-idle-shutdown.md) | Heavy work runs on the box, and the box powers itself off when idle |
| [26-11-03-004](docs/adr/26-11-03-004-agentgateway-mcp-gateway-layer.md) | agentgateway is the MCP front door |
| [26-11-06-001](docs/adr/26-11-06-001-command-jobs-fan-out-across-discobox.md) | Command jobs fan out across Discobox sandboxes, with no agent and no credential |
| [26-11-10-001](docs/adr/26-11-10-001-discobox-019-provisioning-defaults-route-lock.md) | Discobox 0.19: Claude boxes run Discobox's own defaults; the credential route is the lock |
| [26-11-17-002](docs/adr/26-11-17-002-box-stands-up-from-the-repo.md) | The build box stands up from the repo on a blank VM |

## License
Apache-2.0, like the projects it composes.
