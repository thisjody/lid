# The build box stands up from the repo on a blank VM

**Status:** Accepted 2026-11-17. A blank Debian 12 VM converged from this repo and passed every check below, and became the build box.
**Date:** 2026-11-17
**Provenance:** Written at decision time.
**Concepts:** box, stack

## Context
`stack_up.sh` built podman, ToolHive and agentgateway, but it only checked Discobox and the Claude credential; those had been set up by hand. On a box that already has everything, an idempotent `stack_up` finds it all in place, passes, and hides any step that was never written down. The only honest test is a blank VM.

## Decision
- **`scripts/lid/discobox_up.sh`, run as `stack_up` step 0**, does what was done by hand:
  - the box user's linger and kvm group (it restarts the user's systemd manager if that manager started before kvm);
  - Nix from the pinned installer (by sha256);
  - the libkrun runtime from the pinned Discobox commit's flake;
  - the client and server by sha256;
  - the libkrun provider, with Docker disabled and its stuck auto-made pool removed;
  - the `krun` pool as the default, and `claude-code` as the default harness;
  - the harness's two files (below);
  - the WIF refresh script;
  - the Claude token secret routed to `api.anthropic.com`, and its binding.

  Every input is pinned in `stack/stack.yaml` under `components.discobox`.
- **The harness files are in the repo,** `stack/discobox/claude-code/{settings,credentials}.json`. The credentials file's access token is `{"$secret": "CLAUDE_CODE_OAUTH_TOKEN"}`, the bound secret's sentinel, so no secret value is in the repo. Discobox 0.18's `harnesses configure` wrote these files; 0.19 removed `configure`, and a fresh 0.19 harness has none. Without them the agent fails with `authentication_failed` and sends no request.
- **The site's own values live in `stack/site.yaml`, not in the code:** box name, GCP project and zone, address, user, commit identity, and the install ids Discobox prints. The file is not committed; `stack/site.example.yaml` is. `scripts/lid/stack_units.py` merges it over the manifest.
- **Two inputs stay with the operator,** copied in rather than committed:
  - `~/.config/anthropic_wif.env` (four non-secret Anthropic WIF ids);
  - the GitHub read-only token, which `stack_secret.sh` reads from stdin.

  The Anthropic federation rule is keyed on the box's service account, so a new VM under the same account mints a token with no change on Anthropic's side.
- **Replacing the box:** stand the new VM up under `host.rebuild_name` (`LID_INSTANCE`), test it, then rename both VMs while stopped. The hostname follows the instance name at boot. Disks keep their creation names, so take snapshots by `disks[0].source`, never by the VM's name.

## Fresh-box findings (each fixed in `stack_up.sh` / `discobox_up.sh`)
1. `podman version` needs an OCI runtime that isn't installed yet: use `podman --version`.
2. `~/.config/systemd/user` doesn't exist on a fresh box.
3. **A target with no tools ended `stack_up` silently** (set -e + pipefail inside a substitution), with no FAIL line. This was a real bug on any box, hidden because every target always served.
4. `discobox version` never starts the server; any API call does.
5. The server runs as a unit of the user's systemd manager, so it has the manager's groups. Linger starts the manager before the user joins kvm, which leaves the pool unable to open `/dev/kvm`.
6. A fresh project has no default harness, so `disco_job`'s create request (which names none) is refused.
7. The harness files (above).
8. `wif-refresh.service` hardcoded the secret id; it is rendered from the manifest now.
9. `route_lock.py` crashed on a missing secret; it now reports "route lock broken".
10. `stack_secret.sh` exited 1 on success under macOS bash 3.2.
11. The 0.19 JSON shapes: `{"providers": […]}`, `{"pools": […]}`, `{"secrets": null}`. Files added with `--file` are listed under `fileOverrides`, not the 0.18-migrated `configuredFiles`.
12. A thv egress image pulled by `:latest` changes under you: thv's default is rebuilt from alpine edge. It is pinned by release tag and digest (`toolhive.egress_image`, cosign-verified), and `stack_check` treats any other egress image as drift.
13. `discobox rm` can return 0 and leave a box running (state `failed`), which leaks a VM and keeps the host from idling. Fan cleanup confirms each archive and fails the job on a leak.
14. Under parallel load a box shell can take over 3 minutes to come up; `disco_job` waits 10.

## Verification (2026-11-17/18; see VERIFICATION.md)
- **Blank VMs, twice** (the second at Discobox 0.19.1, after 0.19.5 was tried and not taken: stack/GAPS.md G15):
  - `stack_up` converged, and a repeat changed nothing;
  - `stack_test` passed (a)–(i), and `decoy_proof.sh` passed;
  - a Claude job and a fan-out job passed;
  - 3 parallel Claude jobs plus a fan-out passed;
  - no live box remained after.
- **A cold reboot:** every service came up without `stack_up`, and `stack_test` passed.
