# Compose by default: Epistemic's infrastructure is assembled from upstream open source and managed as a pinned stack manifest

**Status:** Accepted
**Date:** 2026-11-03
**Provenance:** Written at decision time.
**Concepts:** box, stack, composition

## Context
The build box already runs one upstream project as infrastructure (Discobox, for job sandboxes). We are about to add a second (ToolHive, for MCP tools). Projects we contribute fixes to are also projects we can run, and running them is the strongest test of a fix. Without a rule, each new component would be installed by hand on the box, pinned nowhere, documented in a session transcript, and patched in place, so the box would drift until nobody could rebuild it.

## Decision
**1. Compose first.** Before building a piece of infrastructure ourselves, check whether an open-source project already does it. If one does, run it, and send whatever it is missing upstream as a contribution rather than forking around it. Conditions:
- keep licence notices;
- no source-available (BUSL and similar) components;
- upstream fixes go upstream;
- never use a project's name as an endorsement;
- each layer checks its neighbour's hand-off (defence in depth by composition).

**2. One manifest is the source of truth.** `stack/stack.yaml` lists every component on Epistemic's own machines, with:
- the upstream repo;
- the pinned version (release tag plus commit SHA, and image digest where there is one);
- what it does for us;
- its ADR;
- its config files in this repo;
- a health check;
- the invariants it is responsible for;
- and, if we run a patched build, the patch and the condition under which we return to the upstream release (`unpatch_when`, usually "upstream PR merged and released").

**3. Install only by script.** `scripts/lid/stack_up.sh` brings the box to the manifest. Nothing is installed or reconfigured on the box by hand, so the box can be rebuilt from this repo.

**4. Invariant tests prove the composition.** `scripts/lid/stack_test.sh` checks the properties the composition claims, not just that services are up. For example:
- no credential is present inside a job sandbox;
- a job can reach only the tools the manifest allows;
- a token without a required scope is refused.

It runs after every change to the stack and before any job batch.

**5. Drift and upgrades are checked, not remembered.** `scripts/lid/stack_check.sh` compares each pin to the latest upstream release, lists new security advisories for each repo, and checks what is installed on the box against the manifest. Its summary prints in the sweep as **▍STACK**. An upgrade is a manifest change on a branch: bump the pin, run `stack_up` and `stack_test` on the box, then merge.

**6. Documentation follows the seam (26-10-24-003).**
- `stack/DESIGN.md` says what is running now, with a diagram.
- Every component gets an ADR when it is added, replaced or materially reconfigured.
- Relationships with the projects' maintainers are not recorded here.

## Consequences
- **The box is cattle, not a pet.** It can be rebuilt from the repo, and box drift is a sweep line rather than a surprise.
- **Running what we contribute to keeps our fixes honest.** A patched component carries its `unpatch_when`, so a local patch can't quietly turn into a permanent fork.
- **Every component adds work.** It brings an upgrade check, a health check and invariant tests, so the bar for adding one is that it does a job we would otherwise build.
- **Not everything is retrofitted yet.** The existing pieces (Discobox, the WIF refresh timer, the idle shutdown) move into the manifest when the first `stack_up.sh` is written. Until then, they are recorded in 26-11-02-001/002 and 26-11-03-003.
