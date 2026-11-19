# lid

lid runs AI coding agents on your code, unattended, without giving them your keys. Each job gets a throwaway sandbox with
no credentials in it. The one key the agent needs, for Claude's API, is added outside the sandbox on the way out, and only
on requests to Anthropic. Every web request a sandbox makes is logged, and the machine turns itself off when nothing is
running.

## The problem

An agent working on its own needs a key to call its model, and often access to tools such as GitHub. If those keys sit
inside the agent's environment, anything the agent runs can read them and send them anywhere: a bad dependency, an
instruction hidden in an issue it reads, or a plain mistake. lid keeps the keys out of the agent's reach instead of
trusting the agent with them.

## How it works

```
your machine ──ssh──▶ build box (a cloud VM, off when idle)
                        │
                        ├─ sandbox, one per job: the agent and your code, with a placeholder where the key would be
                        │     │  every request leaves through
                        │     ▼
                        │  Discobox's proxy ── placeholder swapped for the real token ──▶ api.anthropic.com only
                        │     │  (anything else goes out with no token, and is logged)
                        │     ▼
                        └─ gateway (read-only tools) ──▶ ToolHive tool servers, which hold the GitHub token
```

- **Sandbox.** Each job runs in its own throwaway sandbox made by [Discobox](https://github.com/discobox-ai/discobox): an
  isolated container inside a small virtual machine that Discobox runs for all jobs. It is created for the job and
  archived when the job ends.
- **Placeholder and swap.** Where the agent expects its Claude token, the sandbox holds a placeholder (Discobox calls it a
  sentinel). Discobox's proxy, outside the sandbox, replaces it with the real token as a request leaves. The real token
  never enters the sandbox.
- **Route lock.** The token has a list of places it may be sent, and that list is exactly `api.anthropic.com`. It is set
  on the secret and on its link to the agent, and a job refuses to start if either says anything else. Sent anywhere
  else, the placeholder is refused or arrives as itself; the real token goes only to `api.anthropic.com`.
- **No stored key.** The Claude token comes from workload identity federation: the build box's own cloud identity is
  exchanged for a short-lived token, refreshed on a timer. No long-lived API key exists anywhere (ADR 26-11-02-001).
- **Tools through one gateway.** Agents call tools over MCP (Model Context Protocol, the standard way an agent talks to a
  tool). A job that asks for tools gets one address, [agentgateway](https://github.com/agentgateway/agentgateway), which
  refuses write tools and logs every call. Behind it, [ToolHive](https://github.com/stacklok/toolhive) runs each tool
  server in its own container ([Podman](https://github.com/podman-container-tools/podman), rootless) and keeps that
  tool's credential there, so the job gets an address, never a token.
- **Audit.** Every web request a sandbox makes goes through Discobox's proxy and is logged. A job fails if the log shows
  traffic it should not have.

What a sandbox can still do: an agent sandbox can reach the internet while the agent runs (Discobox's default, which lets
a box install what it needs at boot). So a job's code and output could leave the box; the token cannot.

## Two kinds of job

- **Agent jobs** (`scripts/monitor/disco_job.sh`): give it a public repo URL (or a tarball), a git ref and a prompt file.
  It clones the code, runs Claude Code headless in a sandbox, exports the agent's commits as patches, saves the audit
  summary and archives the sandbox. Up to 6 run at once.
- **Command jobs** (`scripts/monitor/disco_fan.sh`): no agent, no credential and no internet. A command such as a test
  suite is split across up to 8 sandboxes, and the results come back as an archive that is unpacked strictly.

## How it is checked

- `scripts/lid/stack_test.sh` runs checks from inside a locked sandbox: no credential in its environment or files; no
  internet; tools reachable only through the gateway; write tools absent and refused; the gateway's own traffic
  confined; a clean audit.
- `scripts/lid/decoy_proof.sh` has a sandbox send its own placeholder to a listener on the host, three ways (as a
  `Bearer` header, as an `x-api-key` header and in a JSON body). It passes only if the real token never arrives.
- `scripts/lid/route_lock.py --self-test` covers the route lock's cases.

What has been tested, how, and what hasn't, is in `VERIFICATION.md`. The checks ran on the pre-publication commits; this
commit differs from them only in comments, docs and test selection.

## Capacity (measured, see `VERIFICATION.md`)

- On the default pool (6 vCPU, 20 GiB, an n1-standard-8 host) at most **8 sandboxes run at once**: the 9th dies before
  it is healthy. The manifest records this as `pool.max_boxes`, and everything that creates a sandbox counts against it
  under one lock.
- **Agent jobs:** up to 6 in parallel; a 7th is refused before it starts. A backlog of 12 drains 6 at a time with no
  failure.
- **Command jobs:** up to 8 shards (the sandbox ceiling), and `shards x workers` up to 2 x the pool's vCPUs.
- Creating a sandbox took p50 12 s and p99 26 s with 6 at once (24 s and 41 s in an earlier run); host memory peaked at
  7.4 to 11.2 of 30 GB.

## Standing one up

**Requirements.** lid is built for one setup and tested only on it: a Google Compute Engine VM reached over IAP ssh, an
Anthropic workload identity federation rule for the Claude credential, and the Discobox, ToolHive and agentgateway
versions pinned in `stack/stack.yaml`. Other clouds or credential schemes would need changes.

1. **A GCE VM:** Debian 12, n1-standard-8, `--enable-nested-virtualization`, no external address (IAP ssh only), and a service account of its own.
2. **An Anthropic workload identity federation rule** that trusts that service account. Put its four ids in `~/.config/anthropic_wif.env` on the box.
3. **`stack/site.yaml`,** from the example.
4. **Set it up, load the GitHub token, and test:**
   ```
   bash scripts/lid/stack_up.sh
   pbpaste | bash scripts/lid/stack_secret.sh github-readonly --expires YYYY-MM-DD   # a read-only GitHub token
   bash scripts/lid/stack_test.sh
   bash scripts/lid/decoy_proof.sh
   ```
   `stack_up` prints the Discobox install ids on its first run; record them in `site.yaml`.

## What's in the repo

- `stack/stack.yaml`: the manifest. Every component is pinned by tag, commit and sha256, with the rules it must keep.
- `stack/site.example.yaml`: your site's own values (box name, GCP project and zone, address, user, commit identity).
  Copy it to `stack/site.yaml`, which is not committed.
- `stack/DESIGN.md`: what runs now. `stack/GAPS.md`: known gaps. `stack/TEST_LOG.md`: test runs.
- `scripts/lid/`:
  - `stack_up.sh` brings a blank Debian 12 VM to the state the manifest describes, and changes nothing on a second run;
  - `discobox_up.sh` (step 0 of `stack_up`) installs and configures Discobox;
  - `stack_test.sh`, `decoy_proof.sh`, `route_lock.py`: the checks above;
  - `stack_check.sh` compares the pins with upstream releases and advisories, and the box with the manifest;
  - `stack_secret.sh` loads the GitHub token into ToolHive's encrypted store from stdin;
  - `idle_shutdown.sh` powers the box off after 20 minutes idle.
- `scripts/monitor/`: the two job drivers above.
- `scripts/lid_run.sh`: the only path from your machine (called "the desk" in the scripts) to the box, over IAP ssh. It
  checks the remote hostname before running anything.
- `docs/adr/`: the decisions and their reasons.

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
| [26-11-10-001](docs/adr/26-11-10-001-discobox-019-provisioning-defaults-route-lock.md) | Discobox 0.19: agent sandboxes run Discobox's own defaults; the credential route is the lock |
| [26-11-17-002](docs/adr/26-11-17-002-box-stands-up-from-the-repo.md) | The build box stands up from the repo on a blank VM |

## License

Apache-2.0.
