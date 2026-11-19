# ToolHive is the build box's tool layer: jobs reach MCP tools through it, and the credentials stay outside the sandbox

**Status:** Accepted (step 1 built and tested 2026-11-03, see the amendment; step 2 planned). Runtime amended 2026-11-04 (Podman built from upstream). ToolHive v0.62.0 since 2026-11-04 (five advisories; amendment 2026-11-04 b). Amended by 26-11-03-004: the proxies now listen on 127.0.0.1 and jobs reach them only through agentgateway on `<host.internal_ip>:18800`; `disco_job.sh --tools <server>` became `--tools gateway`.
**Date:** 2026-11-03
**Provenance:** Written at decision time.
**Concepts:** box, stack, composition, secrets

## Context
Build jobs run headless in Discobox sandboxes:
- network egress is denied by default;
- no credential enters the box;
- MCP is switched off (`ENABLE_CLAUDEAI_MCP_SERVERS=false`), and the audit checks for MCP traffic.

So a job cannot look anything up. The desk fetches issue threads, PR state and CI logs ahead of time and pastes them into the prompt. A job therefore works from a snapshot, and wrong assumptions about live upstream state cost review rounds.

ToolHive (stacklok/toolhive, Apache-2.0) runs MCP servers in their own containers. A proxy in front can check OIDC tokens and Cedar policies, and secrets are injected at container start from an encrypted store.

## Decision
**Step 1: a tool cupboard on the host, reachable from the sandbox.**
- ToolHive runs on the box host (not inside Discobox) under a container runtime that `stack_up.sh` installs.
- It runs the GitHub MCP server, read-only toolsets only.
- The server's token is a fine-grained, public-repository read-only GitHub token. It is held in ToolHive's encrypted secret store and injected into that one container. It never enters a job sandbox, a job prompt, or this repo.
- A job sandbox gets one egress allow: the ToolHive proxy's internal address and port.
- MCP is enabled in the job's Claude config for that one server only. Hosted connectors stay off.
- `stack_test.sh` asserts:
  - the sandbox environment and filesystem contain no GitHub credential;
  - the sandbox can reach the proxy and nothing else new;
  - the tool can read but not write.

**Step 2: the proxy checks who is calling.**
- The proxy requires an OIDC token, using `--oidc-required-scopes`.
- Tokens come from ToolHive's own embedded authorization server, minted per job with the tool's scope.
- `stack_test.sh` adds:
  - no token gives 401;
  - a token without the scope gives 403;
  - a token with the scope gives 200.

Until upstream ships required-scope enforcement, step 2 runs a pinned build of our patch, recorded with `unpatch_when` in the manifest.

## Consequences
- Jobs read live upstream state themselves. This fixes the snapshot problem, while the "no credential in the box" property of 26-09-25-002 still holds.
- Two independent layers now guard the tool path: Discobox limits where a job can connect, and ToolHive limits what it can do there and holds the key.
- The box host needs a container runtime and more memory headroom. Heavy work stays on the box, and the box stops when idle (26-11-03-003).
- The GitHub token is a long-lived secret on the host (unlike the 10-minute Claude token of 26-11-02-001). It is kept low-value on purpose: read-only, public repos, short expiry. Its expiry date goes in the manifest so `stack_check.sh` warns before it lapses.

## Amended 2026-11-03: what step 1 actually chose
Built and tested on the box the same day. Code: `scripts/lid/stack_{up,test,secret,check}.sh`, `stack_units.py`, `mcp_probe.py`; manifest `stack/stack.yaml`; run log `stack/TEST_LOG.md`.
- **Runtime: rootless Podman (Debian 12 apt, 4.3.1), not Docker.** Discobox's server ships a Docker provider that is disabled on this box but still probes `/var/run/docker.sock` (observed 10/24: with no Docker it loops on that error, and its auto-made Docker pool became the judge pool and stuck). A host Docker daemon would hand that dormant provider a live socket. Rootless Podman serves only `/run/user/1000/podman/podman.sock`, runs as the box user, and adds no root daemon. ToolHive reaches it through its Docker-API client: `TOOLHIVE_RUNTIME=podman` is not a runtime name in v0.61.0, so the units set `TOOLHIVE_PODMAN_SOCKET` instead.
- **ToolHive v0.61.0**, pinned by tag, commit and release-asset sha256. Each MCP server is a systemd user unit running `thv run --foreground --transport stdio`. The transport has to be explicit: run by image reference rather than registry name, thv defaulted to streamable-http and the stdio server exited.
- **Ports and binding.** Each server's proxy listens on the host's internal address only: time on `<host.internal_ip>:18810`, github on `<host.internal_ip>:18811`, path `/mcp` (streamable HTTP). Three layers limit who can reach them:
  - the GCP firewall admits only IAP ssh to the VM;
  - host chain `EPI_STACK` accepts those ports only from the host's own address;
  - it rejects any new connection from uid 1000 (Discobox's proxy and passt) to any other port on <host.internal_ip>.

  So a sandbox allowed `<host.internal_ip>` reaches the proxies and nothing else on the host. Without the chain, the allow would open every host listener (sshd, LLMNR) to the sandbox. Verified: uid 1000 to :22 is refused while root to :22 is open.
- **How the sandbox reaches the proxy.** It goes through Discobox's own egress proxy (`HTTP_PROXY=http://127.0.0.1:<port>` inside the sandbox), which runs on the host. The job's box gets `--egress-allow <host.internal_ip>` at the box level, the same mechanism `lockdown.sh` proved on 10/30, and the harness default stays deny-by-default with an empty allow. `disco_job.sh --tools <server>` adds that allow, writes `/tmp/job/mcp.json` for the one server, and runs `claude -p --mcp-config /tmp/job/mcp.json --strict-mcp-config` with `ENABLE_CLAUDEAI_MCP_SERVERS=false` unchanged.
- **Secrets.** The store is ToolHive's `encrypted` provider (AES-256-GCM, Argon2id key). On a headless host thv keeps the store password in the kernel user keyring (its `keyctl` fallback), which a reboot clears. A random password therefore lives in `~/.config/epistemic-stack/thv_master` (0600) and is re-primed into the keyring by every thv unit's `ExecStartPre`. Setup is non-interactive: prime the keyring, then pipe `encrypted` into `thv secret setup`. This protects a copied store file, not a compromised box user. The boundary that matters is unchanged: the token is injected only into the github container. `stack_secret.sh` carries the value over the ssh session's stdin into `thv secret set`, so it never appears in argv, environment, files or output.
- **Test server.** Registry `time` (`mcp-server-time`, dockyard build) with permission profile `none` (no network). `fetch` and `everything` were rejected: one has network, and the other exposes `get-env`.
- **GitHub server.** `github-mcp-server` v2.1.0 by digest, with `GITHUB_READ_ONLY=1` and toolsets `context,repos,issues,pull_requests,actions`. It runs under ToolHive's isolated network with the registry's allow list (`.github.com`, `.githubusercontent.com`, port 443). Its unit is enabled only once `github-readonly` is in the store. Network isolation pulls two helper images that thv requests by `:latest`, a gap in the pin discipline that `stack_check.sh` reports by digest.
- **End to end:** a `disco_job.sh --tools time` smoke job had Claude Code list and call the tool through the proxy, with 0 hosted-connector traffic and the internet still blocked (`stack/TEST_LOG.md`).
- **Not yet done:** Discobox's own install isn't scripted (`stack_up.sh` verifies its version and lockdown only). Invariant (d), read-only, is SKIP until the token is loaded.

## Amended 2026-11-04: Podman built from upstream, because Debian 12's 4.3.1 broke the isolated github server's egress
**What was wrong.** stack_test (i) failed: `lookup github-egress on 10.89.1.5:53: no such host`. Every live GitHub call failed, while listing tools worked. The backend was not the cause: Debian 12's Podman already ran netavark + aardvark-dns, and aardvark resolved `github-egress` correctly. The cause was this:
- thv `--isolate-network` sets the MCP container's DNS (`--dns`) to its own dnsmasq helper. That helper runs `--no-resolv` with only 1.0.0.1/1.1.1.1 upstream, so it can't resolve container names.
- On Docker, and on Podman 4.4.0 and later, a container's `--dns` servers become the *upstream* of the network's resolver, which answers container names first (podman#16172).
- On Podman 4.3.1, `--dns` replaced aardvark outright.

Debian 12, backports included, has nothing newer, and Kubic has no Debian_12 repo. So fix 1 (a Debian DNS component, or a netavark/aardvark switch) did not apply, and fix 2 was taken.

**What was chosen (`stack.yaml` → `podman`, applied only by `stack_up.sh`):**
- **Podman v5.8.9** (5.x maintained line; 6.x needs netavark/aardvark 2.0). Built on the box from the upstream tag, checked against its pinned commit, with a pinned Go toolchain from go.dev (sha256). Installed to `/usr/local/bin/podman`.
- **netavark v1.17.2**, built from the tag plus its release vendor tarball (sha256) with a pinned Rust toolchain from static.rust-lang.org (sha256). The release binary needs GLIBC 2.39, and Debian 12 has 2.36.
- **aardvark-dns v1.17.1**, the release binary (sha256).
- **crun 1.30.1**, upstream's static build. Debian's 1.8.1 rejects Podman 5.8's OCI spec version. Its `.asc` was verified as Giuseppe Scrivano's key AF60FCA3…, then its sha256 was pinned.
- Helpers live in `/usr/local/libexec/podman`. `~/.config/containers/containers.conf` points `helper_binaries_dir` and the `crun` runtime there and sets rootless networking to slirp4netns, because Debian 12's passt is too old for pasta. The `podman.socket` and `podman.service` user units are generated.
- Debian's podman, netavark and aardvark-dns were purged after `podman system reset`. thv re-created its containers and the storage moved from vfs to native overlay. ToolHive's secret store is thv's own file and was untouched. Runtime deps (conmon, catatonit, uidmap, slirp4netns, containers-common/image) stay on Debian 12 apt.

**Isolation unchanged.** The github container is still only on `toolhive-github-internal` (`internal: true`). Its sole way out is thv's egress proxy with the `.github.com` / `.githubusercontent.com`:443 allowlist. Checked by hand on the internal network:
- via `github-egress`, api.github.com gives 200;
- example.com gives CONNECT 403;
- a direct connection to a GitHub IP has no route.

The only change is that name lookups go to aardvark first, which is the Docker behaviour thv was written for. stack_test (a)–(i) all PASS (`stack/TEST_LOG.md`, 2026-11-04 14:40Z).

**Costs.** Three self-built or upstream binaries replace Debian packages, so security updates no longer arrive through apt. `stack_check.sh` now reports podman, netavark, aardvark-dns and crun against upstream releases and advisories, and checks box drift on all four. A rebuild needs about 1 GB of transient toolchains, which are removed after the build.


## Amended 2026-11-04 (b): ToolHive v0.61.0 → v0.62.0 for five advisories
**Version.** ToolHive v0.62.0: tag, commit `a364591f…` and release-asset sha256 `ada3b4ee…`, recorded in `stack.yaml`. The release API digest and `toolhive_0.62.0_checksums.txt` agree. Installed through `stack_up.sh` only. Run log: `stack/TEST_LOG.md`, 2026-11-04 14:47Z. stack_up converged twice, the second time with no changes, and stack_test (a)–(i) all PASS, including (i), a live GitHub read through the gateway.

**Why: the five advisories published 11/4 against v0.61.0, and what each means for us.**
- **GHSA-9r8f-m39c-5rv5** (medium). Numeric tool arguments escape Cedar `arg_*` limits. **Not reachable today:** we run no Cedar policies, and neither ToolHive proxy has authz configured. It becomes relevant the day step 2's caller checks are written as Cedar rules in ToolHive rather than CEL at the gateway.
- **GHSA-gxq5-f6xj-f7w2** (low). A `read_resource` policy keyed on a URI can be bypassed by respelling the URI. **Not reachable:** we have no Cedar policies. Our time and github servers are Go/Python, and the gateway exposes tools only.
- **GHSA-4rg8-mr2x-646g** (medium). Unbounded Cedar parsing crashes the Kubernetes operator. **Not applicable:** we don't run the operator.
- **GHSA-mq57-h84j-5wfp** (medium). A `parallel()` panic in vMCP code mode. **Not applicable:** we don't run vMCP. agentgateway is our multiplexer.
- **GHSA-7qm5-mcvg-c27h** (medium). A client's `Connection` header strips headers ToolHive injects (upstream token, forwarded headers). **Not affected, on two counts, both checked on the box:**
  - Our github token is a container env var (`--secret github-readonly,target=GITHUB_PERSONAL_ACCESS_TOKEN`). It is not a header the proxy adds, and we use neither `--remote-auth` nor `--remote-forward-headers`.
  - Our servers are `--transport stdio`, so the proxy is thv's stdio bridge, which the advisory lists as unaffected, not the transparent proxy. A loopback request carrying `Connection: X-Probe` got 200 from the github proxy, where v0.62.0's transparent proxy would now answer 400.

  We take the fix anyway: it matters the day we front a remote server with an injected token.

**What changed for us (release notes plus the full v0.61.0..v0.62.0 diff, 16 commits).**
- `thv run --foreground --transport stdio`: unchanged. `pkg/runner` changed comments only, and `pkg/transport/proxy/streamable` changed comments only.
- `TOOLHIVE_PODMAN_SOCKET` and the container runtime: unchanged (no `pkg/container` changes).
- The encrypted secret store and keyring priming: unchanged (no `pkg/secrets` or config changes). The store file was byte-identical across the upgrade and was not re-created, and `github-readonly` still opens.
- The isolated network and the egress/dns helper images: unchanged (no `pkg/networking` changes). The two `:latest` helper digests on the box are the same as before.
- Permission profiles: unchanged (no `pkg/permissions` changes).
- Proxy ports and binding: unchanged. The transparent proxy now returns 400 to a request whose `Connection` header names a non-hop-by-hop header. Our stdio proxies don't use that path, and agentgateway sends only hop-by-hop tokens.
- Everything else in the release is in parts we don't run: the auth server, the vMCP, the operator, Cedar, and `thv llm`.

**Found while upgrading, fixed in our scripts.**
- `stack_up.sh` replaced the thv binary but did not restart the thv units, because their unit files don't change with the version. The proxies would have kept running v0.61.0 in memory. A thv install now restarts every thv unit.
- `stack_check.sh` counted every advisory published after the pinned release. These five were published hours *after* v0.62.0, so it kept flagging them against the release that fixes them. It now skips an advisory whose patched version is at or below the pin. That also cleared the agentgateway (GHSA-qm3r, -7hv4, fixed in our v1.7.0) and crun (GHSA-5f8q, fixed in our 1.30.1) lines, which were stale for the same reason.
