#!/bin/bash
# discobox_up.sh: install and configure Discobox on the box from nothing, to stack/stack.yaml (ADR 26-11-17-001).
# Run ON the box by stack_up.sh (step 0); idempotent, so a box that already has each piece only gets checked.
# Everything is pinned in the manifest: the client and server by sha256, Nix by its installer's sha256, and the libkrun
# runtime by the Discobox commit's flake. Two inputs are not in the repo, because they belong to the box's owner:
#   ~/.config/anthropic_wif.env   the four non-secret Anthropic WIF ids (ADR 26-11-02-001); copied in by the operator
#   the Anthropic federation rule must already trust this box's service account (it is keyed on the account, not the VM)
# Never prints a secret.
set -euo pipefail
R=$HOME/stack/repo; Y=$R/stack/stack.yaml; L=$R/scripts/lid
get(){ python3 "$L/stack_units.py" "$Y" get "components.discobox.$1" "${@:2}"; }
ok(){ echo "ok    $*"; }; chg(){ echo "set   $*"; }; die(){ echo "FAIL  $*"; exit 1; }
export XDG_RUNTIME_DIR=/run/user/$(id -u) PATH=$HOME/.local/bin:/nix/var/nix/profiles/default/bin:$PATH
TAG=$(get pin.tag); SHA=$(get pin.sha)

# 0a. The server user: lingering (the server socket lives under XDG_RUNTIME_DIR) and in kvm (libkrun opens /dev/kvm).
[ -c /dev/kvm ] || die "/dev/kvm missing: create the VM with --enable-nested-virtualization"
loginctl show-user "$USER" -p Linger | grep -q yes || { sudo loginctl enable-linger "$USER"; chg "linger"; }
getent group kvm | grep -qw "$USER" || { sudo usermod -aG kvm "$USER"; chg "kvm group"; }
if ! id -nG | grep -qw kvm; then exec sg kvm -c "bash $L/discobox_up.sh"; fi   # pick up the new group in this run
ok "server user: linger, kvm"
# The server runs as a unit of this user's systemd manager (app.slice/discobox-server-*.service), so it has the manager's
# groups. A manager started before this user joined kvm (linger starts it at once) can't give it /dev/kvm (fresh box, 11/17):
# restart the manager once; its enabled user units come back, and the next client call relaunches the server with kvm.
KG=$(getent group kvm | cut -d: -f3); M=$(pgrep -u "$USER" -x systemd | head -1 || true)
if [ -n "$M" ] && ! grep -E '^Groups:' "/proc/$M/status" | tr -s ' \t' '\n' | grep -qx "$KG"; then
  sudo systemctl restart "user@$(id -u).service"; sleep 3; chg "user manager restarted to pick up kvm"
fi

# 0b. Nix (multi-user), from the pinned installer; only to build the libkrun runtime, which Debian 12 can't package.
if ! command -v nix >/dev/null; then
  U=$(get install.nix.installer); want=$(get install.nix.sha256); f=$(mktemp)
  curl -sSfL "$U" -o "$f"; got=$(sha256sum "$f" | cut -d' ' -f1); [ "$got" = "$want" ] || die "nix installer sha256 $got != $want"
  sh "$f" --daemon --yes >/tmp/nix-install.log 2>&1 || die "nix install (see /tmp/nix-install.log)"; rm -f "$f"; chg "nix $(nix --version)"
fi
ok "$(nix --version)"

# 0c. The libkrun runtime (libkrun with block + network support, and passt), from the pinned Discobox commit's flake.
RT=$HOME/discobox-libkrun-runtime
if [ ! -e "$RT/lib/libkrun.so.1" ] || [ ! -x "$RT/bin/passt" ]; then
  nix --extra-experimental-features 'nix-command flakes' build "github:discobox-ai/discobox/$SHA#libkrun-runtime" \
    --out-link "$RT" >/tmp/libkrun-build.log 2>&1 || die "libkrun runtime build (see /tmp/libkrun-build.log)"; chg "libkrun runtime $(readlink -f "$RT")"
fi
ok "libkrun runtime $(basename "$(readlink -f "$RT/lib")")"

# 0d. The client, by sha256, straight from the release; the client stages its own server, checked by sha256 too.
B=$HOME/.local/bin/discobox; want=$(get install.client.sha256)
if [ ! -x "$B" ] || [ "$(sha256sum "$B" | cut -d' ' -f1)" != "$want" ]; then
  mkdir -p "$(dirname "$B")"; f=$(mktemp)
  curl -sSfL "https://github.com/discobox-ai/discobox/releases/download/$TAG/discobox-linux-amd64" -o "$f"
  got=$(sha256sum "$f" | cut -d' ' -f1); [ "$got" = "$want" ] || die "discobox client sha256 $got != $want"
  install -m 0755 "$f" "$B"; rm -f "$f"; chg "discobox client $TAG"
fi
discobox --auto-start-server=false admin server stage >/dev/null 2>&1 || true
# A server already running at another version (a pin move) keeps running until stopped; stop it so the next call launches
# the staged one. Boxes keep their state across a server restart.
rv=$(discobox --auto-start-server=false version 2>&1 | awk '/server version/{print $NF}')
if [ -n "$rv" ] && [ "$rv" != unavailable ] && [ "$rv" != "$TAG" ]; then discobox --auto-start-server=false admin server shutdown >/dev/null 2>&1 || true; sleep 5; chg "server $rv stopped for $TAG"; fi
SV=$(ls "$HOME/.local/state/discobox/server/linux-amd64/$TAG/"* 2>/dev/null | head -1)
[ -n "$SV" ] && [ "$(sha256sum "$SV" | cut -d' ' -f1)" = "$(get install.server.sha256)" ] || die "discobox server for $TAG missing or not the pinned sha256 ($SV)"
# The only place lid starts a server, and only when none runs (discobox_ensure.sh); every other call refuses to start one.
bash "$(dirname "$0")/discobox_ensure.sh"; for _ in $(seq 1 30); do discobox --auto-start-server=false admin providers ls >/dev/null 2>&1 && break; sleep 2; done
dv=$(discobox --auto-start-server=false version 2>&1 | awk '{print $NF}' | sort -u | tr '\n' ' '); [ "$dv" = "$TAG " ] || die "discobox versions: $dv (pin $TAG)"
ok "discobox $TAG (client + server by sha256)"

# 0e. Providers: the libkrun one is the only enabled provider; Docker (the Linux default, with no daemon here) is disabled.
J(){ discobox --auto-start-server=false "$@" -o json; }
prov=$(J admin providers ls | python3 -I -c 'import json,sys; d=json.load(sys.stdin).get("providers") or []; print(" ".join("%s:%s:%s" % (p["id"],p["type"],int(bool(p.get("disabled")))) for p in d))')
kid=$(tr ' ' '\n' <<<"$prov" | awk -F: '$2=="libkrun"{print $1; exit}')
if [ -z "$kid" ]; then
  kid=$(J admin provider create --type libkrun --name libkrun --libkrun-path "$(readlink -f "$RT/lib/libkrun.so.1")" \
        --passt-path "$(readlink -f "$RT/bin/passt")" --vcpus "$(get pool.vcpus)" --memory-mi-b "$(( $(get pool.memory_gib) * 1024 ))" \
        --data-disk-gi-b "$(get provider.data_disk_gib)" --cache-disk-gi-b "$(get provider.cache_disk_gib)" | python3 -I -c 'import json,sys; print(json.load(sys.stdin)["id"])')
  chg "libkrun provider $kid"
fi
for p in $prov; do IFS=: read -r id ty dis <<<"$p"; [ "$ty" != libkrun ] && [ "$dis" = 0 ] && { discobox --auto-start-server=false admin provider update "$id" --disabled >/dev/null && chg "provider $ty disabled"; }; done
ok "provider libkrun $kid"

# 0f. One pool on it, the default, agents over plain HTTP inside the VM (as the 10/24 install).
PN=$(get pool.name)
pid=$(J admin pools ls | python3 -I -c 'import json,sys; n=sys.argv[1]; d=json.load(sys.stdin).get("pools") or []; print(next((p["id"] for p in d if p["name"]==n), ""))' "$PN")
if [ -z "$pid" ]; then
  pid=$(J admin pool create "$PN" --provider "$kid" --disable-agent-endpoint-tls | python3 -I -c 'import json,sys; print(json.load(sys.stdin)["id"])'); chg "pool $PN $pid"
fi
# Ours is the project default; any other pool (Discobox's auto-made Docker "Default", stuck with no daemon) is removed.
discobox --auto-start-server=false admin pool set-default "$pid" >/dev/null
for o in $(J admin pools ls | python3 -I -c 'import json,sys; k=sys.argv[1]; print(" ".join(p["id"] for p in json.load(sys.stdin).get("pools") or [] if p["id"]!=k))' "$pid"); do
  discobox --auto-start-server=false admin pool rm "$o" >/dev/null 2>&1 && chg "removed pool $o" || echo "warn  could not remove pool $o"
done
for i in $(seq 1 90); do r=$(J admin pools get "$pid" | python3 -I -c 'import json,sys; print(json.load(sys.stdin).get("ready"))'); [ "$r" = True ] && break; sleep 10; done
[ "$r" = True ] || die "pool $pid not ready after 15 min"
ok "pool $PN $pid ready"

# 0f2. claude-code is the project's default harness: disco_job's create request names no harness (10/24 by hand; fresh box 11/17).
# The NAME column has spaces ("Claude Code"), so match the DEFAULT "yes" on the claude-code row, not a column number.
discobox --auto-start-server=false admin harnesses ls 2>/dev/null | awk '$2=="claude-code"' | grep -qw yes || { discobox --auto-start-server=false admin harness set-default claude-code >/dev/null && chg "default harness claude-code"; }
ok "default harness claude-code"

# 0g. The Claude credential by WIF: refresh script, a token secret routed only to api.anthropic.com, and its harness binding.
# Minting runs as its own system user: the box user needs no instance credentials, so the host firewall keeps it off the
# cloud metadata server (defense in depth).
[ -s "$HOME/.config/anthropic_wif.env" ] || sudo test -s /etc/epistemic-stack/anthropic_wif.env || die "~/.config/anthropic_wif.env missing (the operator copies in the four WIF ids)"
id epi-wif >/dev/null 2>&1 || { sudo useradd --system --no-create-home --shell /usr/sbin/nologin epi-wif; chg "system user epi-wif"; }
sudo install -d -m 0755 /etc/epistemic-stack /usr/local/libexec/epistemic
[ -s "$HOME/.config/anthropic_wif.env" ] && { sudo install -m 0640 -o root -g epi-wif "$HOME/.config/anthropic_wif.env" /etc/epistemic-stack/anthropic_wif.env; rm -f "$HOME/.config/anthropic_wif.env"; chg "WIF ids moved to /etc/epistemic-stack (root:epi-wif 0640)"; }
MINT=/usr/local/libexec/epistemic/wif_token.sh
sudo cmp -s "$L/wif_token.sh" "$MINT" || { sudo install -m 0755 -o root -g root "$L/wif_token.sh" "$MINT"; chg "$MINT"; }
SUDO_RULE="$USER ALL=(epi-wif) NOPASSWD: $MINT \"\""
echo "$SUDO_RULE" | sudo cmp -s - /etc/sudoers.d/epistemic-wif || { echo "$SUDO_RULE" | sudo tee /etc/sudoers.d/epistemic-wif.new >/dev/null; sudo chmod 0440 /etc/sudoers.d/epistemic-wif.new; sudo visudo -cqf /etc/sudoers.d/epistemic-wif.new && sudo mv /etc/sudoers.d/epistemic-wif.new /etc/sudoers.d/epistemic-wif; chg "sudo rule: $USER -> epi-wif $MINT"; }
REFRESH="sudo -n -u epi-wif $MINT"
sudo -n -u epi-wif "$MINT" >/dev/null || die "the WIF mint could not mint a token (federation rule, service account, or ids)"
ok "WIF token mints"
SN=$(get claude_secret_name)
# The manifest's id wins when it exists on this server (the running box's secret predates the name); else by name.
want=$(get claude_secret --optional)
sid=$(J secret ls | python3 -I -c 'import json,sys; w,n=sys.argv[1:]; d=json.load(sys.stdin).get("secrets") or []; ids=[s["id"] for s in d]; print(w if w in ids else next((s["id"] for s in d if s["name"]==n), ""))' "$want" "$SN")
if [ -z "$sid" ]; then
  sid=$(J secret create --name "$SN" --type token --refresh-command "$REFRESH" --ttl 5m \
        --format 'sk-ant-oat01-{base64url:95}' --route api.anthropic.com --max-binding-ttl forever | python3 -I -c 'import json,sys; print(json.load(sys.stdin)["id"])')
  chg "claude secret $sid"
fi
# The refresh command first: the WIF ids already moved to /etc, so an old command would fail from here on.
rc_now=$(J secret get "$sid" | python3 -I -c 'import json,sys; print(" ".join(json.load(sys.stdin).get("refreshCommand") or []))')
[ "$rc_now" = "$REFRESH" ] || { discobox --auto-start-server=false secret update "$sid" --refresh-command "$REFRESH" >/dev/null && chg "claude secret refresh command -> $REFRESH"; }
nb=$(J admin binding ls | python3 -I -c 'import json,sys; s=sys.argv[1]; print(sum(1 for b in json.load(sys.stdin).get("secretBindings") or [] if b.get("secretId")==s))' "$sid")
[ "$nb" -gt 0 ] || { discobox --auto-start-server=false secret bind "$sid" --harness claude-code --as CLAUDE_CODE_OAUTH_TOKEN --route api.anthropic.com --for forever >/dev/null; chg "binding claude-code CLAUDE_CODE_OAUTH_TOKEN"; }
# The harness's configured files (stack/discobox/claude-code/): settings, and a credentials file whose access token is the bound
# secret's sentinel ({"$secret": "CLAUDE_CODE_OAUTH_TOKEN"}), so Claude Code in the box authenticates through the proxy swap.
# 0.18's `harnesses configure` wrote them on 10/24; 0.19 removed configure, and a fresh harness has none (fresh box, 11/17:
# the agent failed `authentication_failed` without one request to api.anthropic.com). No secret value is in either file.
HF=$R/stack/discobox/claude-code
need=$(J admin harnesses get claude-code | python3 -I -c '
import json,sys; d=json.load(sys.stdin); hf=sys.argv[1]
have={f["path"]: f.get("content") for f in (d.get("configuredFiles") or []) + (d.get("fileOverrides") or [])}   # 0.18-migrated, then 0.19 --file
want={".claude/settings.json": hf+"/settings.json", ".claude/.credentials.json": hf+"/credentials.json"}
norm=lambda t: json.loads(t) if t else None
print(" ".join(p for p,f in want.items() if norm(have.get(p)) != json.load(open(f))))' "$HF")
for p in $need; do f=$HF/settings.json; [ "$p" = .claude/.credentials.json ] && f=$HF/credentials.json
  discobox --auto-start-server=false admin harnesses update claude-code --file "$p=@$f" >/dev/null && chg "claude-code file $p"; done
ok "claude-code configured files"
[ "$sid" = "$(get claude_secret --optional)" ] || die "the Claude secret here is $sid; set components.discobox.claude_secret to it in stack/site.yaml"
ok "claude secret $sid bound to claude-code"
