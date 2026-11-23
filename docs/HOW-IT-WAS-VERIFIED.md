# How lid was verified

lid was built mostly by AI coding agents, and it runs unattended with real credentials. "The tests pass" is not enough
for code like that: an agent writes the tests for its own code, and both can share a blind spot. So lid went through a
fixed set of checks before it was published. The numbers are in [`VERIFICATION.md`](../VERIFICATION.md).

## The checks

| check | what it answers |
|---|---|
| **Tests that can fail** | Each critical guard is broken on purpose, and its test must go red. A test that cannot fail proves nothing. |
| **Suite and property tests** | Every test passes; Hypothesis generates thousands of random inputs for the credential route lock and the unpacker. |
| **Mutation testing** | mutmut plants thousands of small bugs; each one the tests miss is killed with a new test or explained. |
| **Blind review** | A fresh AI reviewer agent, one that did not write the code, reads the whole tree against the rules it must keep. |
| **Install from nothing** | A new VM, a fresh clone, only the documented setup: install, test, run real jobs, leave nothing behind. |
| **Chaos soak** | Three hours of jobs while the sandbox server, a tool server and the gateway are killed, the host reboots and the network drops mid-launch. |
| **Load test** | Step up parallel jobs until something gives, and write the ceiling down. |
| **Honest docs** | Every claim matches a run, and the docs say what was not checked. |

## Where they sit in the ladder

The usual ladder for checking agent-written software has four rungs. lid's checks cover all four:

1. **Unit tests:** tests that can fail, the suite and property tests, and mutation testing.
2. **Model as judge:** the blind reviews, which are AI agents grading the code against written rules.
3. **Throwaway apps:** the install from nothing, with real jobs against the real services.
4. **Sandboxes under real conditions:** the chaos soak and the load test.

One step sits between rungs 2 and 3: proving the judge. lid's blind reviewers were **not** calibrated against held-out
cases. Nobody measured how often they miss a planted defect or flag a clean one. Their findings were real and were fixed,
but a clean review is not a measured result. [judge-kit](https://github.com/thisjody/judge-kit) is the procedure for that step.

## The rules around them

- **Define "pass" before running.** The soak's criteria are hashed before the first job. When one soak failed on a single
  unreadable status check, the criterion stayed; the harness was changed to retry and record why, and the soak ran again.
- **Test the code you ship.** A change to running code reruns the checks.
- **Every fix goes through the loop:** a failing test first, then the fix, the suite, and a blind review of the fix.
- **Say what was not done.** The security scan ran once, before its one finding was fixed, and was not rerun on the fix.
  The soak of the final code ran half its three hours before the test account ran out of credit, and the AI reviewers
  were not calibrated against held-out cases.

## What it found

The unit tests passed throughout. These came from the live runs and the blind reviews:

- **A fresh install could silently break every agent job.** One new sandbox host installed the coding agent incomplete,
  and every sandbox got a stub. lid now checks and repairs this at setup, and jobs fail fast if the agent cannot run.
- **Real capacity was 8 sandboxes, not the 12 configured.** The 9th fails to start. The ceiling is now enforced.
- **One chaos test never tested anything.** It waited a fixed time and every launch finished first. It now confirms
  where each interruption landed.
- **The reviews found a result copy with no size limit, an audit that stopped at its page limit and read as clean, and
  cleanup paths that could leave a sandbox running.**
- **A hostile sandbox could exhaust the host's memory through the unpacker.** The security scan found that Python's tar
  reader loads an archive's extended headers in full before lid's own size limits apply. Closing it took five rounds of
  blind review, each finding what the last fix missed. The last miss showed only on the gate's own machines, whose
  patched Python reads headers by another path. The unpacker now refuses every header type plain `tar` never writes.
- **A busy sandbox server could be replaced, killing every job on the host.** Under load, one of lid's own calls took the
  slow server for dead and started a second one, which shut the first down. Found when it happened on the build box
  during another project's work. No lid call starts a server now; one timer restarts it only when none runs, and the
  soak's own server kills turned up two more mistakes in that fix before it held.
- **A killed fan-out job's boxes were archived without an audit.** The last whole-tree review found that the cleanup
  sweep only audited Claude jobs. It now audits every box it archives, and the long checks ran again on the fix.

## What it cost

Most of the value came from the live runs, the blind reviews and mutation testing. The cost was reruns: every small fix
sent the long checks round again. The lesson is to keep the loop strict and save the long runs for the end.
