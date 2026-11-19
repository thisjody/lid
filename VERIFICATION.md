# How lid is tested

What has been checked, how, and what has not. The raw run logs stay on the operator's machine (they carry site values);
the numbers below come from them.

## Checks

| check | how | result |
|---|---|---|
| Unit, regression and property tests | `tests/gate/suite.sh` in a Discobox box: every script through `bash -n`, every Python file compiled, the route lock's self-test, and the full pytest suite with Hypothesis at its gate profile (at least 2,000 generated cases per property) | pass |
| Tests that can fail | `tests/red/red_checks.py`: each critical guard (the credential route lock, the safe unpacker, the lock check, the leak check, the egress pins) is removed or inverted in turn, and its test must go red | pass |
| Mutation testing | mutmut 3 over the guard modules, the soak verdict and the orphaned-job sweep: 2,407 mutants caught, 185 survived; the survivors are listed below by class | done |
| Install from nothing | a new Debian 12 VM, a fresh clone and a site file: `stack_up` installs and configures everything, and a second run changes nothing; `stack_test` passes (a)-(k); the decoy proof passes; a real Claude job and a fan-out job pass; no box is left running | pass |
| Load | 1, 2, 4 and 6 parallel Claude jobs; a 7th refused by the cap; a backlog of 12 drained 6 at a time; a fan-out at the pool's box ceiling (8) | pass, figures below |
| Chaos soak | 3 hours of continuous Claude and fan-out jobs while the Discobox server is killed, the GitHub tool server restarted, the gateway killed, the host rebooted and the ssh tunnel dropped mid-launch | pass, details below |
| Code review | repeated blind reviews of the whole tree by a fresh reviewer against the invariants below, and of every fix | findings fixed or listed under *Known limits* |
| README walkthrough | a clean clone: every path the README names exists, every script parses, the example site file loads, the route-lock self-test passes | pass |
| Security scan (claude-security) | | **not run** |

The invariants every check is aimed at: no credential inside a sandbox; the Claude credential is routed only to
`api.anthropic.com`; command boxes are deny-by-default; every box is audited and archived on every path; the box caps
hold under concurrency; the box never powers itself off under a running job.

## The soak

241 jobs over 3.03 hours on a box stood up from a blank VM, with the pass criteria hashed before the first job:

- 239 jobs passed. The 2 that failed did so inside a fault window: one fan-out failed while the Discobox server was down,
  and one fan-out worker was cut off by the reboot and closed after boot by the orphaned-job sweep.
- Each of the five faults was proven to have happened, and after each one a Claude job and a fan-out job passed.
- All 122 passing Claude jobs exchanged the credential, and only with `api.anthropic.com`.
- The decoy proof passed 4 times out of 4: a box sends its own credential sentinel to a sink on the host, and the real
  token never arrives.
- No box outlived its job, and none was live at the end.

The soak ran before three small changes found by the last review (a size cap on fan-out result pulls, treating a full
audit page as truncated, and keeping no response body in the decoy's probes). Those changes passed the install
from nothing, the load test, the suite, the red checks and mutation testing, but have not been soaked.

## Load

On the default pool (6 vCPU, 20 GiB, on an n1-standard-8 host), with the final code:

| step | result | box create p50 / p99 | live boxes at most |
|---|---|---|---|
| 1, 2, 4 jobs | all pass | 6 / 6 s, 6 / 8 s, 10 / 12 s | 1, 2, 4 |
| 6 jobs (the Claude cap) | all pass | 12 / 26 s | 6 |
| 7 jobs | 6 pass, 1 refused at preflight | 13 / 26 s | 6 |
| backlog of 12 | all pass, 6 at a time | 12 / 25 s | 6 |
| fan-out, 8 shards (the box ceiling) | pass | 36 s | 8 |

Host memory peaked at 7.4 of 30 GB. An earlier run on a pre-publication commit passed the same steps (box creation p50/p99
24 / 41 s at 6 jobs, memory 11.2 GB). The pool starts 8 boxes and the 9th dies before its sandbox agent is healthy, so the
manifest records `pool.max_boxes: 8` and every box creator counts against it.

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
- in `route_lock.py`, mutants inside `self_test()`, which is itself a test.

4 survivors in the orphaned-job sweep's audit wrapper have not been triaged.

## Known limits

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
- Very large prompts can hit the shell's argument limit on the ssh command line; `stack_ship` sends the shipped tree as one
  ssh argument (about 35 KB under the Linux limit).
- `lid_run.sh` sets no connect timeout; wrap long runs in `timeout`.
- Smaller items from the reviews: an unreadable status poll is excused by a fault window by its start time only; a box
  create the client timed out may still finish on the server after the cleanup's listing; the canary's failed-create
  cleanup removes any box that appeared under the lock, including one made by hand in that window; `stack_test` waits for
  its foreground command before acting on TERM.
