# Discobox build jobs run the agent headless, up to six in parallel, with commits authored as the box owner

**Status:** Accepted
**Date:** 2026-11-02
**Provenance:** Written at decision time.
**Concepts:** box, watch, secrets

## Context
`scripts/monitor/disco_job.sh` (first written 10/30, with no record until now) drove each Discobox job by operating Claude Code's interactive TUI:
- it typed the prompt into the primary terminal;
- it dismissed an auto-mode modal;
- it scraped the screen footer to tell busy from idle;
- it nudged once if the agent stopped early.

It also refused to start while any other box was live, so jobs ran one at a time.

Under the federated credential, the TUI path fails: interactive Claude Code marks its requests as a subscription client, and the API answers "credit balance too low". Headless `claude -p` with a bearer token runs as plain API usage, which the credits cover and which Anthropic documents as covered.

A dry run also showed the commits from a box with no git identity are authored "Claude <noreply@anthropic.com>". Patches go upstream under the box owner's hand, so every patch would have needed re-authoring.

## Decision
- **Headless agent.** After setup, the driver writes `/tmp/job/agent.sh` into the box and starts it as its own systemd unit (see the amendment below; `setsid nohup` did not survive). The script sets a fresh `CLAUDE_CONFIG_DIR` and passes the harness-templated sentinel (from `~/.claude/.credentials.json`) as `ANTHROPIC_AUTH_TOKEN`, so the real token is still swapped in only by the proxy. It runs `claude -p "Read /tmp/job/PROMPT.md and follow it exactly." --dangerously-skip-permissions --output-format stream-json`. If no `DONE` appears, it runs one `--continue` nudge, then writes `/tmp/job/agent.exit`.
- **Completion.** The desk-side loop polls every 20 seconds:
  - `DONE` ends the job successfully;
  - `agent.exit` without `DONE` fails the job (exit 10) and saves the agent's error tail;
  - the `--timeout-min` limit still applies.

  The agent transcript (`agent.jsonl`) is exported with the patches.
- **Parallel.** The one-box-at-a-time refusal becomes a cap: `MAXBOX=6` live boxes. Each job already has its own `~/scratch/<job>` tree, box and run log, so boxwatch sees every job.
- **Authorship.** Before the agent runs, the driver sets the box's global git identity to the box owner. Patches come back already authored correctly. The rule against an AI `Co-Authored-By` trailer stands.
- **Unchanged.** The lockdown preflight now checks the federated secret's route, not the subscription secret's. Also unchanged: deny-by-default harness egress, per-box `--allow`, the audit summary with connector-traffic VIOLATION, and the `== stage` run-log lines.

## Consequences
- **Simpler and sturdier driver.** It no longer depends on screen scraping, modal handling or TUI footer text, which broke with each Claude Code UI change.
- **Amended 2026-11-02 evening: how the agent is launched and guarded.** The first long job, coder-probe, showed four things.
  - **"Detached" wasn't.** Each `discobox shell` runs as a `discobox-exec-*` systemd unit with `KillMode=control-group` and `TasksMax=138`. A `setsid nohup` child dies with that unit, without writing `agent.exit`, so the desk loop waits until timeout. Under the task cap a Go build also fails with "fork failed".
  - **New launch.** The driver now starts the agent with `sudo systemd-run --unit=job-agent -p TasksMax=infinity --collect` as the box user. The shell's exported environment, which includes the egress proxy variables, is carried through `/tmp/job/env.sh`.
  - **Longer command limits.** Claude Code's 2-minute default Bash timeout killed a cgo build (exit 137). The driver sets `BASH_DEFAULT_TIMEOUT_MS=1800000` and `BASH_MAX_TIMEOUT_MS=3600000`.
  - **Idle watchdog.** If `agent.jsonl` stops growing for 10 minutes and the agent has no child process running a tool, the agent is killed and the one `--continue` nudge resumes it.
- **Interactive debugging is gone.** There is no live terminal to watch mid-job. `agent.jsonl` is the record instead.
- **Credit use is capped.** Six parallel jobs draw from the plan's monthly credits, with no card on file, so spend can't exceed them. Proven 11/2 with two parallel dry runs: 5/5 `v1/messages` 200, no connector traffic, patches exported.
- **The cap is a guess.** Six is sized to the n1-standard-8 box and has not been load-tested at six. Raise it only after watching memory under a full run (see 26-10-15-001).
- **The nudge is cruder.** One `--continue` replaces "idle 3 minutes, then nudge". An agent that exits early gets exactly one more pass.
