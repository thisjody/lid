# How lid is tested

What has been checked, how, and what has not. The raw run logs stay on the operator's machine (they carry site values);
the numbers below come from them.

## Checks

| check | how | result |
|---|---|---|
| Unit, regression and property tests | `tests/gate/suite.sh` in a Discobox box: every script through `bash -n`, every Python file compiled, the route lock's self-test, and the full pytest suite with Hypothesis at its gate profile (at least 2,000 generated cases per property) | pass |
| Tests that can fail | `tests/red/red_checks.py`: each critical guard (the credential route lock, the safe unpacker, the lock check, the leak check, the egress pins) is removed or inverted in turn, and its test must go red | pass: 33 of 33 |
| Mutation testing | mutmut 3 over the guard modules, the soak verdict and the orphaned-job sweep: 2,779 mutants, 2,565 caught, 210 survived, 4 with no test; the survivors are listed below by class | done |
| Install from nothing | a new Debian 12 VM, a fresh clone and a site file: `stack_up` installs and configures everything, and a second run changes nothing; `stack_test` passes (a)-(k); the decoy proof passes; a real Claude job and a fan-out job pass; no box is left running | pass |
| Load | 1, 2, 4 and 6 parallel Claude jobs; a 7th refused by the cap; a backlog of 12 fed 6 at a time by the load harness (lid itself does not queue); a fan-out at the pool's box ceiling (8) | pass, figures below |
| Chaos soak | 3 hours of continuous Claude and fan-out jobs while the Discobox server is killed, the GitHub tool server restarted, the gateway killed, the host rebooted and the ssh tunnel dropped mid-launch | **not complete on the final code**: 1.5 hours, 4 of 5 faults, no unexcused failure; stopped when the test account ran out of credit (details below) |
| Code review | repeated blind reviews of the whole tree by a fresh reviewer against the invariants below, and of every fix; the last whole-tree review is described below | findings fixed or listed under *Known limits* |
| README walkthrough | a clean clone: every path the README names exists, every script parses, the example site file loads, the route-lock self-test passes | pass |
| Security scan (claude-security) | the claude-security plugin (0.12.0), whole repository, medium effort: an inventory and threat model, 14 researchers by component and vulnerability class, then a three-voter panel on every candidate. Run first at `ada41e5`, then again on the final code at `ed729f7` | first run: 1 finding, LOW, fixed (see *The security finding*). Final run: 0 findings; all 96 files accounted for, 14 of 14 researchers returned, the one candidate rejected 0 to 3 (see *The final scan*) |

The invariants every check is aimed at: no credential inside a sandbox; the Claude credential is routed only to
`api.anthropic.com`; command boxes are deny-by-default; every box is audited and archived on every path; the box caps
hold under concurrency; the box never powers itself off under a running job.

## The soak

**The soak of the final code is not complete.** It ran 1.5 of its 3 hours on `bb9e26b`, on a box stood up from a
blank VM with the pass criteria hashed before the first job, and was stopped when the Claude account behind the test
credential ran out of credit (the API began refusing every Claude job; nothing in lid caused it). Up to that point:

- 95 jobs ran: 91 passed, 3 were still running when it stopped, and the 1 that failed was a fan-out worker cut off by
  the reboot and closed after boot by the orphaned-job sweep (inside that fault's window, as the criteria allow).
- Four of the five faults were proven to have happened, each with a Claude job and a fan-out job passing after it:
  the Discobox server killed, the GitHub tool server restarted, the gateway killed and the host rebooted. The fifth, an
  ssh tunnel dropped mid-launch, had not yet run.
- All 47 passing Claude jobs exchanged the credential, and only with `api.anthropic.com`.
- The decoy proof passed twice out of twice.
- The boxes still live when it stopped belonged to the jobs still running, and the orphaned-job sweep archived them.

The two soaks before it, on the commits that first made every call refuse to start a server, failed: with no client
restarting a killed server, cleanups in the minute before it came back recorded leaks and unreadable audits that were
not real, and the first version of the restart timer killed the server it had just started. Those are fixed (see *The
server that replaced itself*), and on `bb9e26b` the server kill produced no failure at all.

An earlier full soak, on a commit before the unpacker fix and the later changes, ran 241 jobs over 3.03 hours with every
fault proven, the same credential and decoy results, and no box outliving its job.

## The security finding

The scan found one thing, confirmed by all three voters: the unpacker that brings a sandbox's results onto the host
(`scripts/monitor/disco_fan_lib/safe_untar.py`) let Python's tar reader load an archive's extended headers in full
before lid's own limits applied. A sandbox's agent is root inside its box and controls those bytes, so a few MB of
archive could push the shared host out of memory. Nothing could be read or changed; the harm was downtime.

The fix went through five rounds of blind review. Each found something the last fix missed:
- **1:** limiting each header's size was not enough. The reader also loaded sparse-file maps, copied global headers into
  every later entry and kept every entry, and a negative size slipped past the byte limit.
- **2:** on Python 3.11, the reader's own pax parser could take about 1 GiB of memory, or 13 s of CPU, per small header,
  before any check saw what it had parsed.
- **3:** 64 KB long names cost minutes of CPU over a large archive, and a corrupt header part-way through ended the
  archive with success.
- **4 and 5:** the corrupt-header check never ran on patched Python builds, the host's among them, which read headers by
  another path. The box's own gate run showed it; tests on the laptop could not. The fifth review then found that
  nothing would notice if that path's check were removed, so a red check now covers it.

The unpacker now refuses, before Python parses them: every pax header (plain `tar` never writes one), sparse files, long
name headers over 4097 bytes, more than two long-name headers in a row, negative sizes, and corrupt or truncated headers.
It no longer keeps entries it has finished with. The reviewers built 38 test archives; replayed on the host's own Python,
each of the 31 hostile ones is refused in under 0.1 s, the 7 benign ones unpack, and memory never went above 32 MB. Archives from the real producer still
unpack: GNU tar 1.35 in a sandbox wrote a 376-character path, a live Claude job exported its commit, and a 140,000-file
archive unpacked (20 s of CPU). The suite, the red checks and mutation testing ran on the final commit.

The two rejected candidates: the agent runs without permission prompts, which is by design in a sandbox with no
credential in it; and the credential route is locked by host alone, which would matter only if Discobox swapped the
credential into a field the API echoes back, which nobody showed.

## The final scan

The scan was rerun on the final code (`ed729f7`, the fixed unpacker included) on 2026-11-23: the whole repository,
96 files in four groups (the box scripts, the fan-out and job workers, the stack and docs, the tests), every top-level
directory accounted for, all 14 researchers returned. Nothing survived the panel. Its one candidate was that a job
run with the gateway's GitHub tools could read issue or PR text written by anyone; all three voters rejected it, because
the tools are opt-in, a job already clones a public third-party repo, the box's egress is open by design, and the
GitHub token is read-only on public repos, so the path gives an attacker nothing they lacked. The scan reads code; it
runs nothing. The researchers did not read the Discobox CLI (not in this repo) and took the host firewall rules from the
design docs.

## The last whole-tree review

A fresh reviewer read the whole tree after the unpacker fix. It found one break of an invariant: when a fan-out job's
worker was killed or the host rebooted mid-job, the orphaned-job sweep archived that job's boxes without auditing them,
because only Claude jobs carried an audit script. A deny-by-default failure in such a run would have gone unrecorded (the
job was still marked failed). The sweep now stops the shard and audits each fan-out box: every exchange must have been
refused, apart from Discobox's own pool services, and an audit it cannot read or judge in full is a violation, as is a box
of a job of neither kind. The same review found box-controlled text copied to the host without a cap (the agent's stderr
tail, the export's stderr, the status poll); those are now capped at 64 KB, 64 KB and 64 bytes. Both fixes went through
failing tests first, four new red checks and a blind review of the fix, whose two small findings were fixed too.

The final code (`ed729f7`) had one more fresh whole-tree review on 2026-11-23, by a reviewer told only the invariants.
It approved: no defect at medium or above, no lock left held, no box left running on any exit path, no way for a
credential to reach a sandbox. Its four low findings were three docs that overstated (the backlog line and the idle
window, corrected here; `stack/DESIGN.md`'s Discobox row and invariant range, listed under *Known limits*) and the fan-out preflight gap listed under
*Known limits*.

## The server that replaced itself

While a second project ran six Claude jobs on the build box, the Discobox server slowed down (database queries of up to
a second). One of lid's own `discobox` calls took it for dead and, as the client does by default, launched a new server;
the new server's first act was to shut the running one down, and every live box's agent died with it. Only the fan-out
worker and the idle check passed `--auto-start-server=false`. Now every lid call does, and a test fails on any call
that does not. One small script, run at boot and every 15 seconds by a user timer, starts a server only when no server
process runs (its pool VM launcher, which shares the name and outlives a killed server, does not count), one run at a
time. Cleanup now waits for a server that answers before it audits or archives, and takes an audit, an archive or a
listing that failed once more after the server is back; a real audit failure is never retried. The fix went through
four blind reviews and three soaks: the reviews found that the first check matched the launcher and that its tests read
the script's text instead of running it, and the soaks found that cleanups racing the restart recorded leaks that were
not real and that the timer's own unit killed the server it had started. All are fixed, with tests that run real
processes.

## Load

On the default pool (6 vCPU, 20 GiB, on an n1-standard-8 host), with the final code:

| step | result | box create p50 / p99 | live boxes at most |
|---|---|---|---|
| 1, 2, 4 jobs | all pass | 6 / 6 s, 6 / 6 s, 10 / 11 s | 1, 2, 4 |
| 6 jobs (the Claude cap) | all pass | 12 / 24 s | 6 |
| 7 jobs | 6 pass, 1 refused at preflight | 13 / 24 s | 6 |
| backlog of 12 | all pass, 6 at a time | 11 / 25 s | 6 |
| fan-out, 8 shards (the box ceiling) | pass | 40 s | 8 |

Host memory in use peaked at 19.3 of 30 GB (the pool VM's memory included). Earlier runs on code before the last
reviews passed the same steps (box creation p50/p99 between 12 / 26 s and 16 / 29 s at 6 jobs). The pool starts 8 boxes
and the 9th dies before its sandbox agent is healthy, so the manifest records `pool.max_boxes: 8` and every box creator counts against it.

## Found by running it

- **A fresh pool can provision a broken Claude Code.** Discobox installs the agent once per pool and reuses it; on one new
  pool npm silently skipped the native optional dependency, and every new box got a placeholder `claude`. Clearing the
  pool's cache made it provision again. `scripts/lid/claude_canary.sh` checks a fresh Claude box: `stack_up` repairs
  (one cache clear, only with no other box live), `stack_test (k)` checks, and a Claude job fails at preflight when its
  agent does not run.
- **The pool's box ceiling is 8** (see *Load*).

## Mutation survivors

None changes a pass/fail decision:
- message text, which the tests match by substring;
- default arguments the tests always override;
- subprocess timeouts and output-capture variants that only matter for a hung process;
- `.get(key, default)` defaults for fields that are always written;
- `split(':', 1)` against other splits on `/proc/meminfo` lines, which carry one colon;
- a redundant check repeated by a later, more general one;
- `fan_cap < 1` against `<= 1`, which differs only for a pool that runs one box;
- in `route_lock.py`, mutants inside `self_test()`, which is itself a test;
- in the orphaned-job sweep: the missing-host default in the fan-out audit (no default can end in the internal domain),
  the encoding-error mode for `job.conf`, an unreadable `job.conf` read as a fan-out job (it is then audited as one), and
  the result keys of an unreadable log, which only the sweep's own summary prints. Its audit wrapper's survivors all end
  in a VIOLATION by another path;
- in the unpacker, the public `frombuf` hook (4 with no test) and the negative-size message: the gate's patched Python
  never reaches them. Both are checked on unpatched 3.11 and 3.13 by `tests/test_inprocess.py`.

Two unpacker survivors are real gaps in the tests, not in the code: opening the archive without `r:gz` (which would also
accept plain, bzip2 or xz tars) and reading a member in one call instead of in 1 MB chunks both pass the tests.


## Known limits

- `stack/DESIGN.md` is behind the code in two places: Discobox is installed by `stack_up.sh` step 0 (`discobox_up.sh`,
  pinned version, sha256 checked), not by hand, and `stack_test` checks invariants (a)-(k), not (a)-(i).
- A fan-out job's preflight refuses a secret binding on the `shell` harness or project-wide, but not one scoped to a
  pool or a box; the Claude jobs' route lock refuses those. A sandbox still holds only the placeholder, egress is
  deny-by-default and the audit fails the job on any exchange.
- `--tools gateway` on a Claude job does not force the agent to use the gateway.
- Sandboxes reach host loopback ports above 1023 through passt (Discobox needs it for source delivery); `stack_test (j)`
  checks that every such listener belongs to Discobox, passt or ToolHive, and the firewall refuses the ToolHive proxies.
- The box user has passwordless sudo on GCE; the network boundary is by uid.
- DNS lookups from a sandbox are not in the egress audit.
- IPv6 is absent on the test hosts; the IPv6 rules are written but not exercised by real traffic.
- Whether Discobox's own loopback listeners require authentication is not verified.
- The Claude Code version and ToolHive's isolation images float (`stack/GAPS.md` G10, G14); every job records what ran.
- The box ceiling (8) was measured with fan-out boxes; a mix of Claude and fan-out boxes at 8 was not measured, and why the
  9th box fails inside the pool VM is not identified.
- Right after a host boot, Discobox's listing can briefly report archived boxes as live. Jobs started in that minute are
  refused by the cap, and the orphaned-job sweep can record a false late LEAK against finished jobs whose boxes were
  already archived (seen across many old jobs after one boot; the boxes were not live).
- A worker killed without a host reboot leaves its job open until the next sweep (at most about 7 minutes).
- Very large prompts can hit the shell's argument limit on the ssh command line. `stack_ship` sends the shipped tree (the
  git-tracked files and the site file) as one ssh argument, about 82 KB against Linux's 128 KB limit for one argument;
  a much larger `scripts/lid` would hit it.
- `lid_run.sh` sets no connect timeout; wrap long runs in `timeout`.
- The unpacker's CPU grows with the archive: up to 200,000 entries with 4 KB names cost about 26 s, and a million empty
  gzip members about 5 s. The 512 MB compressed cap on a box's export bounds it.
- The unpacker stops at the first all-zero block, as GNU tar does, so anything after one is ignored. A path over about
  1,500 directories deep is refused (Python's recursion limit); the producer never writes one.
- The sweep judges a fan-out box it audits after a killed worker more leniently than the worker would: every exchange
  must have been refused, but refused attempts after the job started do not fail it (the worker's own cut-off time is not
  carried to the sweep). It reads the box's whole audit into memory, as the worker does.
- A Claude agent is root in its box; the sweep and the worker stop its unit before the audit, but a process it started
  outside that unit could still send between the audit and the archive. The credential route lock still holds then.
- A host shutdown is read from `/run/nologin` or the system state; a job that finishes during a scheduled shutdown that
  is later cancelled is left for the sweep, which closes it as failed.
- If the decoy proof is SIGKILLed, its temporary firewall rule (the sandbox user to one host port) stays until the next
  firewall restart or reboot.
- If the IPv6 firewall cannot load, `stack_up` stops at that step without saying so (jobs refuse to start, because the
  firewall is inactive).
- A Claude job whose box will not archive after a successful export reports the leak as `export exit 3 (unexpected)`;
  the export's files are complete. Seen on the build box after a server restart.
- After the Discobox server restarts, a cleanup waits for it to answer a listing, not for it to have re-read every pool;
  an audit that still comes back incomplete on its second try is recorded as a violation (it fails safe).
- A server that dies and stays down for the whole of a cleanup's 3-minute wait leaves that job's boxes for the
  orphaned-job sweep, recorded as leaks.
- Smaller items from the reviews: an unreadable status poll is excused by a fault window by its start time only; a box
  create the client timed out may still finish on the server after the cleanup's listing; the canary's failed-create
  cleanup removes any box that appeared under the lock, including one made by hand in that window; `stack_test` waits for
  its foreground command before acting on TERM.
