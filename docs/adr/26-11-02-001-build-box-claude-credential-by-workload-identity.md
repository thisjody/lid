# The build box authenticates to Claude by workload identity federation; no static key exists

**Status:** Accepted
**Date:** 2026-11-02
**Provenance:** Written at decision time.
**Concepts:** box, secrets

## Context
Build jobs run in Discobox sandboxes on the build box (the build box). Until now the sandboxes' Claude credential was the box owner's subscription login, stored as a Discobox OAuth secret and swapped in by Discobox's egress proxy, so the sandbox only ever saw a sentinel. Two things pushed for a change:
- Running several build jobs at once on a personal subscription is not ordinary individual use.
- The plan now includes monthly API credits, which apply only to API usage from a linked Console organization.

The obvious path was a static Console API key. It is a long-lived secret that has to be stored, rotated, and can leak. The Console offers workload identity federation (WIF), in which a cloud workload trades a short-lived identity token from its own provider for a short-lived Claude token. The box is a GCE instance with a dedicated, user-managed service account, which is the case Anthropic's GCP guide covers.

## Decision
The build box gets its Claude credential through WIF, and no static key exists anywhere.
- **Anthropic side.** One Console federation issuer (`https://accounts.google.com`, discovery JWKS). One rule matches the audience `https://api.anthropic.com` plus the box service account's exact `sub` and `email` claims and the GCP project. It targets one Console service account in one workspace, with scope `workspace:developer` and a 600-second token lifetime. There is no subject prefix or wildcard: Google `sub` values have no stable prefix, so a wildcard would admit any GCP service account.
- **Box side.** `bin/wif_token.sh` on the box fetches a Google-signed identity token from the metadata server (`format=full`), exchanges it at `POST /v1/oauth/token` (RFC 7523 jwt-bearer), and prints the access token. It reads only non-secret IDs from `~/.config/anthropic_wif.env`. Source: `scripts/lid/wif_token.sh` (deployed to `~/scratch/discosetup/bin/` on the box).
- **Discobox.** A `token`-type secret whose `--refresh-command` is that script, with a 5-minute TTL, routed to `api.anthropic.com` only, under a standing grant scoped to the claude-code harness config. The harness's `CLAUDE_CODE_OAUTH_TOKEN` binding points at it, so sandboxes receive only the sentinel and the proxy swaps the live token in on the way out.
- **Rollback.** The old subscription-login secret is kept. Rebinding `CLAUDE_CODE_OAUTH_TOKEN` to it restores the previous credential.

## Consequences
- **No long-lived credential to store, rotate or leak.** The real token lives at most 10 minutes, only in Discobox's control plane, and only one GCP service account in one project can mint it.
- **Token expiry mid-job is handled, by our timer, not by Discobox.** *(Amended 2026-11-02 evening; the original line said Discobox refreshes every 5 minutes, which is wrong.)* Discobox runs a token's refresh command only when someone asks by name (`discobox secret refresh <id> --run`; the command is the project's, so the server never runs it unprompted). When the value goes stale, a box's use turns into a pending refresh request and its API calls fail. The 11/2 dry runs passed only because they finished inside the first token's 10 minutes; the first long job (coder-probe) got 0 of 2 `v1/messages`. Fix: `scripts/lid/wif-refresh.{service,timer}`, a systemd user timer on the box (linger on) that runs the refresh every 4 minutes, and `disco_job.sh` refuses to start unless that timer is active and a refresh succeeds.
- **New secrets need a standing grant before a sandbox can use them.** Without one, the sandbox's request sits `pending` and the API call goes out unauthenticated (401). This is a Discobox default that is easy to miss.
- **The credits cover API-mode traffic only.** Interactive Claude Code requests carry a subscription-client beta header, and the API refuses them with "credit balance too low". That forced the driver change recorded in 26-11-02-002.
- **The design is general.** Any GCP-hosted worker of ours (Cloud Run entities included) can authenticate the same way: add one federation rule per service account, never a shared key.
