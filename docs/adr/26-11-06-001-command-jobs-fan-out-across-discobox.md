# Command jobs fan out across Discobox sandboxes, with no agent and no credential

**Status:** Accepted 2026-11-06. R1–R2 answered; t1–t10 pass (stack/TEST_LOG.md); blind review clean after 3 rounds (nothing at ≥80). Outstanding: the claude-security scan of these drivers (see VERIFICATION.md for what has run on this sha); known gaps G8–G12 in stack/GAPS.md.
**Date:** 2026-11-06
**Provenance:** Written at decision time.

**Concepts:** box, stack

## Context
ADR 26-11-02-002 gave the box one kind of Discobox job: a headless Claude agent, one per box, from a public repo (`scripts/monitor/disco_job.sh`). The operator's shipped-code gate (a checklist kept outside this repo) needs a second kind: plain commands (a test suite, mutation shards, a build) fanned out across several boxes from private or local source, with nothing from the agent harness.

The 11/6 mutation runs did this with a one-off orchestrator and hit six gaps:
- **G1:** there's no driver for command-only fan-out jobs;
- **G2:** there are no mounts, so source goes in by git push and results come out by `cat`;
- **G3:** pip can't reach an index from inside a box;
- **G4:** parallelism is capped by the pool VM's 6 vCPU, not by the number of boxes;
- **G5:** every box starts the Claude harness with a credential binding it doesn't need;
- **G6:** idle shutdown can't see box work.

Without a supported path, gate work drifts back to the bare host, which is the failure the gate exists to prevent.

## Decision (proposed; the open questions are marked)
1. **`scripts/monitor/disco_fan.sh`: one driver for command jobs (G1).**
   - Input: `<job> <repo-dir> <sha> <cmd-script> --shards N [--workers W] [--deps req.txt] [--timeout-min M]`. `<source>` is a local git repo and a sha, shipped to the lid as `git archive <sha>`, never a credentialed clone.
   - It runs the same lockdown preflight as `disco_job.sh`, creates N boxes (capped by § 4), gives each box `SHARD=i/N`, runs the script under an in-box unit, waits for `/tmp/job/DONE`, pulls results, saves each box's audit and archives the box.
   - `logs/run.log` gets `== stage` lines on every path, so `boxwatch.sh` works unchanged.
2. **Results out in one call (G2).**
   - Each box tars `/tmp/job/out`, and the driver pulls it back with a single `discobox` call into `~/scratch/<job>/out/shard-<i>/`.
   - Source stays git-in.
   - *Open (R1):* does Discobox v0.18.x, or v0.18.1, offer a copy-out or volume primitive that's better than `shell … cat`?
3. **Packages without egress (G3).**
   - Default: the driver builds a wheelhouse on the host from a pinned requirements file, for the Python version *read from the box image*. It ships it in the archive and installs with `--no-index`.
   - No package index is reachable from a box.
   - *Open (R2):* is a pinned local mirror worth having (devpi, or a proxy allow limited to `pypi.org` and `files.pythonhosted.org`)? Default answer: no, because an index reachable from the sandbox is a new egress path, and the wheelhouse needs none.
4. **Parallelism from the manifest (G4).**
   - `stack.yaml` records the pool's vCPU count. The driver sets shards × workers ≤ 2 × pool vCPU and refuses beyond that.
   - *Open (R1):* can the provider be resized to the host's 8 vCPU, and what does that cost while host work (soaks) runs alongside?
5. **A plain box for command jobs (G5).**
   - Command jobs run in a box with no Claude harness and no secret binding, with an empty egress allow.
   - The driver checks each box's audit and FAILS the job on any outbound request other than `git.discobox.internal`.
   - *Open (R1):* how does Discobox v0.18.x express a no-harness box (a harness or template setting, a project default, a CLI flag)?
6. **Idle shutdown sees box work (G6).**
   - `idle_shutdown.sh` counts the box as busy while any Discobox box is live (`discobox ls`), or while any unit matching `job-*` or `disco-*` is active.
   - The driver runs under `systemd-run --unit=disco-fan-<job>`.

## Research results (R1, R2; 11/6) and what they settle
- **R1 (Discobox v0.18.0, probed with one throwaway box `sbx_7rahykf033sxk85c`, sources at tag `8f87ada`):**
  - `discobox cp` copies both ways, binary-safe (sha256 matched for random, all-byte and control-byte files). It takes `-r` for directories. So G2 is mostly not a gap: source, the wheelhouse and results all move by `cp`, and there is no throwaway repo.
  - The built-in `shell` harness has **no bound secret and no image egress**. `discobox admin box create --harness shell --egress-deny-by-default --no-project-skills --wait -o json` gives a box locked from its first second, with no global config change. In the probe it made 0 requests at startup. It runs as root, and Python is 3.13.5 on Debian 13.
  - The pool VM size comes from the provider (6 vCPU, 20 GiB). Resizing **replaces the VM and kills every live box**, so it is done only with zero live boxes. There are no per-box CPU limits (upstream removed them, their ADR 0029). A second pool is the only CPU partition.
  - Live boxes: `discobox --auto-start-server=false ls -o json`, with the predicate taken from upstream `model.go`: `state == awaiting_source`, or `runtime.state` in starting/running/stopping and `state` not archived or deleted. Root runs it via `runuser -u <host.user>` with `XDG_RUNTIME_DIR=/run/user/1000`, in 0.1 s.
  - **New gap G7:** a box stops itself after 30 minutes with no attached client or keepalive lease, CPU notwithstanding (upstream ADR 0108). A long shard takes a lease, `touch -d '+N hours' /run/discobox/keepalive/<job>`, and removes it at the end.
  - Audit: `discobox admin audit http --discobox-id <id> --limit 1000000 -o json`. Blocked attempts are recorded (`pypi.org:443 403 host denied`). The record is incomplete if `unavailablePools` is non-empty.
  - v0.18.1 only hardens host tools (no `DISCOBOX_TOKEN`). It isn't needed here, and upgrading re-seeds harness images.
- **R2 (packages):**
  - The wheelhouse stays the supported path. A mirror or a PyPI allow would add an egress path; a pre-baked image is a later speed-up only.
  - Resolve for the box's exact Python and glibc first (`uv pip compile --python-platform x86_64-manylinux_2_<glibc> --python-version <v> --generate-hashes`), then `pip download --require-hashes --no-deps --only-binary=:all:` with **every** manylinux tag from the box's glibc down to 2_17, plus the 2014/2010/1 aliases. One tag misses wheels.
  - Install in the box with `--no-index --require-hashes --no-deps`. A one-byte tamper is rejected.

**Decisions, now settled:**
- (2) Results come out by `discobox cp`.
- (3) The wheelhouse goes in by `cp`, built on the host as R2 describes.
- (4) The cap holds: shards × workers ≤ 2 × pool vCPU, read from `stack.yaml`. A resize waits for zero live boxes.
- (5) Command boxes use the `shell` harness through `admin box create --egress-deny-by-default`. The audit check FAILS on **any** exchange, blocked attempts included. Preflight checks the shell harness has 0 bound secrets and no image egress hosts.
- (6) `idle_shutdown.sh` uses the `ls` predicate via `runuser`; on any error it reports busy, never idle.
- **(7) new:** the in-box runner holds a keepalive lease for the job's timeout.

## Built (11/6) and what review changed
- **Layout:** `scripts/monitor/disco_fan.sh` (desk launcher) plus `scripts/monitor/disco_fan_lib/`: `worker.sh` (the lid, as system unit `disco-fan-<job>`), `box_run.sh` (in-box, unit `fan-shard`), `safe_untar.py`, `lockcheck.py`.
- **Blind review findings, all fixed and tested:**
  - **A shard's result** is now systemd's `ExecMainStatus` from a *loaded* unit with a recorded exit, never a job-writable file. `RemainAfterExit=yes` keeps a successful unit readable, and a missing unit is LOST. When the unit ends, its cgroup is killed. t8: a forged `rc=0` from a leftover process had no effect.
  - **Box archives are untrusted:** regular files and directories only, relative paths, no `..`, no links, written with `O_EXCL|O_NOFOLLOW`, into a new `box/` dir. Host files sit beside it. t8: a planted symlink to a host file was refused, and the file was unchanged.
  - **The lock check** compares hashes as well as pins, and refuses unrecognized lines.
  - **Shards are stopped before the audit.**
  - **Unrecorded boxes** are found by name and archived.
  - **The outer unit** runs with `KillMode=mixed` and `TimeoutStopSec=15min`.
  - **Idle shutdown** parses strictly, treats unknown states as live, and reads a failed check as busy.
  - **Numeric inputs** are capped in length.
- **Found by running the tests:**
  - the box glibc (2.41) is newer than uv's newest tag (2.40), so the resolve steps down;
  - shards ran as root and a permission test diverged, so shards now run as `job`;
  - the proxy env exists only in interactive `discobox shell` sessions, so a shard unit has no route out at all. A job that finds the proxy itself is blocked and recorded (t2).
- **Remaining limits** are in `stack/GAPS.md` (G8–G12).

## Verification (the tests, designed before the build)
- **(t1) Equivalence:** a Python project's test suite at a fixed sha, sharded across N boxes by `disco_fan.sh`, gives the same pass, fail and xfail set as one host run.
- **(t2) Lockdown:** every command-job box's audit shows 0 requests to api.anthropic.com and any other host except `git.discobox.internal`. A planted `curl https://pypi.org` in the job script makes the job FAIL.
- **(t3) No credential:** no secret binding on a command-job box, and no key material in `~/scratch/<job>`.
- **(t4) Results:** a deliberately failing shard is reported as FAIL with its output pulled back; a shard that times out is archived and reported, never left running.
- **(t5) Idle:** with a live box and no ssh, `idle_shutdown.sh --check` reports busy; with none, idle; with `discobox` failing, busy. A shard that runs longer than 30 minutes, with nothing attached, completes (lease).
- **(t6) Capacity:** a request for more shards × workers than the cap is refused with the cap named.
- **(t8) Hostile job:** a planted symlink in the output is refused and the host file is untouched; a leftover process forging success has no effect; the job is FAIL.
- **(t9) Lost shard:** a shard unit stopped or removed mid-run reads LOST and fails, never rc=0.
- **(t10) Leftovers:** a process the job leaves behind can't change `out/` after the result; the root runner kills every `job` process first.
- **(t7) Regression:** `stack_test.sh` invariants (a)–(i) still pass, and `disco_job.sh` agent jobs are unchanged.
- **Gate:** Tier 2 of that gate: suite, a code review on the scripts, the claude-security scan (with the owner's yes), docs, and every fix re-entering.

**Amendment 2026-11-18 (load test, see VERIFICATION.md).** G4 was half right: the pool is capped by boxes as well as CPU. It starts 8 boxes and the 9th dies before its sandbox agent is healthy, so `stack.yaml` records `pool.max_boxes: 8`. The driver keeps shards × workers ≤ 2 × pool vCPU and adds shards ≤ `max_boxes`; the worker's live-box check, which used the vCPU cap of 12, now uses `max_boxes`.

## Consequences
- Gate work (suites, mutation, builds) runs through the stack by default, in parallel, with a recorded audit. Bare-host runs become the logged exception.
- A second driver to maintain beside `disco_job.sh`. Shared preflight and cleanup are factored out rather than copied.
- Live checks that need a credential or non-git egress (soaks against a real service) are still host work, until ADR 26-11-03-002 step 2 (per-job egress plus gateway key injection) lands.
