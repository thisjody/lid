#!/bin/bash
# wif_token.sh: print a short-lived Claude API access token for the build box, minted by
# Workload Identity Federation. There is no stored key anywhere.
#   1. The GCE metadata server signs a Google identity token for the box's own service
#      account, with audience https://api.anthropic.com.
#   2. Anthropic exchanges it (RFC 7523 jwt-bearer) for an sk-ant-oat01 token that acts as
#      the Console service account the federation rule targets.
# Discobox runs this as the secret's --refresh-command, so a sandbox only ever holds a
# sentinel, and the real token lives at most token_lifetime_seconds (600) in the control plane.
# Runs as the system user epi-wif (sudo -u epi-wif, from the secret's refresh command): the box user
# may not reach the metadata server at all (host firewall: defense in depth).
# IDs only (no secrets) come from /etc/epistemic-stack/anthropic_wif.env (root:epi-wif 0640):
#   ANTHROPIC_FEDERATION_RULE_ID ANTHROPIC_ORGANIZATION_ID ANTHROPIC_SERVICE_ACCOUNT_ID ANTHROPIC_WORKSPACE_ID
set -euo pipefail
set -a; . /etc/epistemic-stack/anthropic_wif.env; set +a
jwt=$(curl -fsS -H "Metadata-Flavor: Google" \
  "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/identity?audience=https://api.anthropic.com&format=full")
JWT=$jwt jq -n '{grant_type:"urn:ietf:params:oauth:grant-type:jwt-bearer", assertion:env.JWT,
  federation_rule_id:env.ANTHROPIC_FEDERATION_RULE_ID, organization_id:env.ANTHROPIC_ORGANIZATION_ID,
  service_account_id:env.ANTHROPIC_SERVICE_ACCOUNT_ID, workspace_id:env.ANTHROPIC_WORKSPACE_ID}' |
  curl -fsS https://api.anthropic.com/v1/oauth/token -H "content-type: application/json" -d @- |
  jq -er .access_token
