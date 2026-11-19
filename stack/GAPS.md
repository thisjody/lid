# Stack gaps (open)

Gaps found by running real work through the composed stack (`DESIGN.md`). Each one says what's missing, what we do instead, and the suggested fix. When a fix lands, its record moves to an ADR or `DESIGN.md` and the entry is deleted here.

## Closed 2026-11-06 by ADR 26-11-06-001 (`scripts/monitor/disco_fan.sh`; tests in `TEST_LOG.md`)
G1–G7 came from the first Discobox gate runs (mutation testing of a Python project, 6 boxes, 22–24 min, against about 3 hours on the bare host).
- **G1:** there was no driver for command jobs.
- **G2:** results now come out by `discobox cp` and are unpacked as untrusted.
- **G3:** a hash-pinned wheelhouse, checked against the lock.
- **G4:** capacity is capped from the pool vCPU in `stack.yaml`.
- **G5:** a `shell` harness box with 0 secrets, egress denied from creation, the shard as unprivileged `job`.
- **G6:** idle shutdown sees live boxes and `disco-*` units, and an error reads busy.
- **G7, found in research:** a box stops itself after 30 idle minutes; shards hold a keepalive lease.

## Still open
### G8. The keepalive lease is not yet proven on a shard longer than 30 minutes
- **Missing:** t5 shows the lease exists with the right expiry. No run has yet had a shard that outlived the 30-minute idle stop with no attached client. The driver polls every 20 s with `discobox shell`, which may itself count as activity, so the poll could be what is keeping boxes alive.
- **Fix:** a one-box run of a 40-minute sleep with polling paused for 35 minutes. It must finish rc=0.

### G9. Two drivers share preflight and cleanup by copy, not by code
- **Missing:** `disco_job.sh` (agent jobs) and `disco_fan.sh` (command jobs) each carry their own lockdown preflight, audit and archive logic. ADR 26-11-06-001 proposed factoring them out. That was deferred so `disco_job.sh` stayed byte-identical (test t7).
- **Fix:** move the shared parts into `scripts/monitor/disco_fan_lib/`, have `disco_job.sh` source them, and re-run `stack_test.sh` and one agent job.
- **Progress 2026-11-10:** the Claude credential's route lock is now ONE script, `scripts/lid/route_lock.py` (self-test 11 cases; red on each of four mutants but one equivalent), used by `disco_job.sh` (shipped into each job), `stack_up.sh` and `stack_test.sh`, with the secret id in `stack.yaml` (`discobox.claude_secret`). Audit and archive logic are still two copies.

### G10. Harness bundles and the agent version float per box (reshaped by Discobox 0.19)
- **Was:** the `shell` harness image moved with the server version and no job recorded its digest.
- **Now (0.19, ADR 26-11-10-001):** harness images are retired. Every box applies its bundles at boot and installs Claude Code from npm (latest) and node from nodesource, so two Claude jobs a day apart can run different agent versions (2.1.316 on 0.18; 2.1.322 on 11/10).
- **Done 2026-11-10:** every job records what it ran. `disco_job.sh` writes `out/versions.txt` (claude-code harness revision, bundle and manifest digests, `claude --version`, `node --version`); `disco_fan.sh` writes the shell harness's. Proven on `dbx19-dry5` and `dbx19-fan-ok4`.
- **Found 2026-11-18 (fixed):** Discobox provisions Claude Code once per pool and reuses it. On a fresh pool npm silently skipped the package's platform-native optional dependency, so `claude` in every new box was the package's 500-byte placeholder ("claude native binary not installed") and every Claude job failed. No new release was involved (npm `latest` was 2.1.327, published 11/17). `discobox admin pool clear-cache` made the pool provision again. `scripts/lid/claude_canary.sh` now checks a fresh Claude box: `stack_up` runs it with `--repair` (one cache clear, under the box-cap lock, only with no other box live), `stack_test (k)` runs it as a check, and `disco_job.sh` fails a job at preflight when its agent does not run.
- **Open:** holding an agent version. The bundle's `install-cli.sh` installs `@anthropic-ai/claude-code` at latest and the harness config has no version field; a hold means our own bundle (`--extends`) or Discobox's agent store (their ADR 0114). Until then, every job's `out/versions.txt` records what ran.

### G11. One pool for everything
- **Missing:** command jobs and agent jobs share one 6-vCPU pool VM. There are no per-box CPU limits upstream.
- **Measured 2026-11-18 (load test):** the pool starts 8 boxes; the 9th dies before its sandbox agent is healthy (container exit 255), reproduced. `stack.yaml` records `pool.max_boxes: 8`; Claude jobs stop at 6, fan jobs at 8 shards, and `stack_test`, `decoy_proof` and the Claude canary check the ceiling under the same lock. The cause inside the pool VM is not identified, and the ceiling was measured with fan (shell) boxes only.
- **Fix, when contention shows:** a second pool on the same provider for command jobs (`discobox admin pool create … --cpu-vcpus N`), named in `stack.yaml`. Resizing replaces the VM and kills live boxes, so do it only with zero live boxes.

### G12. Credentialed live checks still run on the host
- **Missing:** soaks and load runs against a real service need its key and a non-git egress, so they can't run in a box.
- **Fix:** ADR 26-11-03-002 step 2, a per-job egress allow plus agentgateway injecting the key at the door.

### G13. Command-job boxes carry one Python (3.13), so a multi-version matrix can't run in Discobox
- **Missing:** the `shell` harness image has only `/usr/bin/python3.13`. `uv` is present but can't fetch interpreters with egress denied, and `disco_fan.sh` builds its hash-pinned wheelhouse for that one interpreter. Found 11/7 on a multi-Python test matrix: 3.13 ran, and 3.10, 3.11 and 3.12 could not.
- **What we do instead:** run the other versions on the lid host with `uv` (a logged stack gap), or on the laptop for a light suite.
- **Fix:** a custom harness image (by digest; see G10) carrying uv-managed 3.10–3.13, plus a per-version wheelhouse in the driver (`--python X.Y`).

### G14. ToolHive's network-isolation images are pulled by `:latest`
- **Missing:** `thv --isolate-network` pulls `dockurr/dnsmasq:latest` and `stacklok/toolhive/egress-proxy:latest` itself and takes no digest.
- **Done 2026-11-10:** `stack.yaml` `toolhive.aux_images` records the digests we reviewed; `stack_check.sh` flags any change with `!` (proven red with a planted digest). A changed image is reviewed, then recorded.
- **Open:** a hard pin, if ToolHive ever accepts one.

### G15. Discobox 0.19.5 breaks parallel Claude jobs; Discobox is held at 0.19.1 (found and decided 2026-11-17)
- **0.19.5, on our workload:**
  - `stack_test` (a)–(i), the decoy proof, a single Claude job and a fan-out all PASS;
  - with several boxes made at once: 2 of 3 parallel Claude jobs never ran an agent (Claude Code's npm postinstall didn't complete in the box; 0 API calls), and once box creation refused the job's source (`pin refs: git update-ref ... cannot lock ref 'refs/namespaces/<box>-primary/refs/heads/master': reference already exists`).
- **0.19.1 control,** on a box built from this repo: 9 of 9 parallel Claude jobs across three runs, after the shell-wait fix below; no install or source failures.
- **Decision:** the pin stays at v0.19.1. Re-test the next Discobox release with `decoy_proof.sh`, 3 parallel `disco_job.sh` jobs plus a `disco_fan.sh`, and a leak check (no live box after the jobs finish).
- **Found on both versions and fixed in our drivers:**
  - `discobox rm` can return 0 and leave a fan box running in state `failed`: a leaked VM that also keeps the host from idling. `worker.sh` now confirms the archive from the listing and retries; a box never archived fails the job as `leak-<id>`. It was seen once on the next run: archived on try 2.
  - `disco_job.sh` waited only ~3 min for a box shell; with several boxes provisioning at once that is too short (11/10 dbx19-dry2, 11/17 fin-par2). It now waits 10 min.
