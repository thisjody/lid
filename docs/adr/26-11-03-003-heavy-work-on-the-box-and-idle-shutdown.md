# Heavy build and test work runs on the build box, and the box powers itself off when idle

**Status:** Accepted
**Date:** 2026-11-03
**Provenance:** Written at decision time.
**Concepts:** box, stack

## Context
On 11/3 the desk stopped the build box to save cost and ran five parallel code reviews on the 8 GB laptop. Two of them were each generating docs with `swag init` (~7 GB apiece) at the same time. The laptop went into heavy swap (load 36), and another desk had to kill one run. The box is an n1-standard-8 (8 vCPU, ~30 GB) at about $0.38/hour on demand. Its disk costs about $6/month whether it runs or not. Leaving it on by mistake is the cost risk; running it when needed is not.

## Decision
- **Heavy work goes to the box.** Builds, test suites, code generation, containers and database tests run on the build box through `scripts/lid_run.sh`, never on the laptop. The laptop only reads diffs and runs light, single-package checks.
- **The box is off when not in use.** The operator stops it when its work finishes.
- **Backstop.** Root cron runs `/usr/local/sbin/idle_shutdown.sh` every 5 minutes. After 4 consecutive idle checks it powers the box off: 15 minutes after the first idle check, so 15 to 20 minutes after the box goes idle. Idle means:
  - no logged-in or ssh session;
  - no `job-agent` unit running;
  - 1-minute load under 0.5.

  A guest power-off leaves the instance TERMINATED, which bills disk only.

## Consequences
- The laptop stays responsive, and the heavy steps run faster on 8 cores.
- A run that detaches from ssh and goes quiet can be cut off by the backstop. On 11/3 it powered the box off under a reviewer that was polling from the laptop. Long runs must stay attached, or run as a `job-agent` unit, which counts as busy.
- The box takes about 30 seconds to start, so each batch of box work pays that once.
- The idle script moves into the stack manifest (26-11-03-001) when `stack_up.sh` is written.

## Amended 2026-11-07: all execution runs on the box, not only "heavy" work
**Provenance:** Written at decision time.
