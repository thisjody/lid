#!/bin/bash
# stack_up.sh: bring the build box to stack/stack.yaml (+ stack/site.yaml) (ADR 26-11-03-001). Idempotent; safe to re-run after any manifest change.
# Run from the desk: `bash scripts/lid/stack_up.sh` ships stack.yaml + scripts/lid to ~/stack/repo on the lid and runs there.
# The box must be RUNNING (gcloud compute instances start <host.name> --project <host.project> --zone <host.zone>).
#
# What it converges (ADR 26-11-03-002 for the ToolHive parts):
#   1. podman built from the pinned upstream tag (Go pinned), netavark (source, Rust pinned), aardvark-dns, crun (pinned binaries);
#      runtime deps from Debian 12 apt; rootless containers.conf + podman.socket (user unit). Replaces Debian's 4.3.1 (podman#16172)
#   2. thv at the pinned release, verified against the pinned sha256 before install
#   3. ToolHive's encrypted secrets provider, set up non-interactively (master password file, primed into the kernel keyring)
#   4. host firewall: the gateway port takes only the host's own address; uid 1000 reaches no other port on it and not the
#      ToolHive proxies on loopback; only the gateway's system user opens those (ADR 26-11-03-004)
#   5. one systemd user unit per MCP server (proxies on loopback); servers needing a secret start only once it is in the store
#  5b. agentgateway: pinned binary, repo config checked against the manifest, hardened system unit as its own user
#   6. the pieces already on the box: idle shutdown cron, wif-refresh timer, Discobox version and lockdown (verified)
# Never prints a secret. Exit nonzero if anything fails to converge.
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/stack_lib.sh"
stack_ship stack_up.sh "$@"

# ---- on the box ----
export XDG_RUNTIME_DIR=/run/user/$(id -u) PATH=$HOME/.local/bin:$PATH; export TOOLHIVE_PODMAN_SOCKET=$XDG_RUNTIME_DIR/podman/podman.sock
R=$HOME/stack/repo; Y=$R/stack/stack.yaml; L=$R/scripts/lid
get(){ python3 "$L/stack_units.py" "$Y" get "$1"; }
ok(){ echo "ok    $*"; }; chg(){ echo "set   $*"; }; bad(){ echo "FAIL  $*"; fail=1; }; fail=0
[ "$(get host.internal_ip)" = "$(hostname -I | awk '{print $1}')" ] || { echo "FAIL  host IP is not $(get host.internal_ip)"; exit 1; }
IP=$(get host.internal_ip)

# 0. Discobox from nothing (ADR 26-11-17-001): nix + the libkrun runtime, client/server by sha256, provider, pool, Claude secret + binding.
bash "$L/discobox_up.sh" || bad "discobox install/config (discobox_up.sh)"

# 1. podman, built from the pinned upstream tag (ADR 26-11-03-002, amendment 2026-11-04: Debian 12's 4.3.1 let --dns replace
# aardvark-dns, podman#16172), with upstream netavark + aardvark-dns pinned by sha256 and Debian 12 apt for the runtime deps.
# python3-yaml is needed by stack_units.py itself; it is on the box already.
PT=$(get components.podman.pin.tag); PS=$(get components.podman.pin.sha); PB=$(get components.podman.install)
PH=$(get components.podman.helpers_dir); PKGS="$(get components.podman.packages) $(get components.podman.build.packages)"
need=; for p in $PKGS; do dpkg -s "$p" >/dev/null 2>&1 || need="$need $p"; done
if [ -n "$need" ]; then
  sudo DEBIAN_FRONTEND=noninteractive apt-get update -qq
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq $need >/dev/null; chg "apt:$need"
fi
sudo apt-mark manual $PKGS >/dev/null   # so purging the replaced apt podman never autoremoves a runtime dep
# `podman --version` needs no OCI runtime; `podman version` fails until crun and containers.conf exist (fresh box, 11/17).
pver(){ "$PB" --version 2>/dev/null | awk '{print $NF}'; }
if [ "v$(pver)" != "$PT" ]; then
  # One-time move off Debian's podman: stop everything on it, wipe its storage (thv re-creates its containers, networks
  # and images on the next unit start; ToolHive's secret store is thv's own file and is untouched), then purge it.
  old=$(for p in $(get components.podman.replaces_apt); do dpkg -s "$p" >/dev/null 2>&1 && echo "$p"; done | tr '\n' ' ')
  if [ -n "$old" ]; then
    systemctl --user stop 'thv-*.service' podman.socket podman.service 2>/dev/null || true
    command -v podman >/dev/null && podman system reset --force >/dev/null 2>&1 || true
    sudo DEBIAN_FRONTEND=noninteractive apt-get purge -y -qq $old >/dev/null; chg "apt purged: $old(storage reset)"
  fi
  B=$HOME/stack/build; rm -rf "$B"; mkdir -p "$B"
  GA=$(get components.podman.build.go.asset); GS=$(get components.podman.build.go.sha256)
  curl -fsSL -o "$B/$GA" "https://go.dev/dl/$GA"
  echo "$GS  $B/$GA" | sha256sum -c --quiet || { rm -rf "$B"; echo "FAIL  go toolchain checksum mismatch"; exit 1; }
  tar -xzf "$B/$GA" -C "$B"
  git -c advice.detachedHead=false clone -q --depth 1 --branch "$PT" "$(get components.podman.upstream)" "$B/podman"
  [ "$(git -C "$B/podman" rev-parse HEAD)" = "$PS" ] || { rm -rf "$B"; echo "FAIL  podman $PT is not commit $PS"; exit 1; }
  ( cd "$B/podman" && PATH=$B/go/bin:$PATH GOPATH=$B/gopath GOCACHE=$B/gocache GOTOOLCHAIN=local \
      make -s BUILDTAGS="$(get components.podman.build.tags)" bin/podman bin/rootlessport >"$B/build.log" 2>&1 ) \
    || { tail -20 "$B/build.log"; echo "FAIL  podman build"; exit 1; }
  sudo install -m 0755 -o root -g root "$B/podman/bin/podman" "$PB"
  sudo install -D -m 0755 -o root -g root "$B/podman/bin/rootlessport" "$PH/rootlessport"
  sudo chmod -R u+w "$B" 2>/dev/null; rm -rf "$B"; chg "podman $PT built from $PS"
  "$PB" system migrate >/dev/null 2>&1 || true
fi
[ "v$(pver)" = "$PT" ] && ok "podman $PT ($PB)" || bad "podman $(pver), pin $PT"
for h in netavark aardvark-dns crun; do
  HT=$(get "components.podman.helpers.$h.tag"); HU=$(get "components.podman.helpers.$h.upstream")
  hv(){ "$PH/$1" --version 2>/dev/null | awk 'NR==1{print $NF}'; }
  if [ "$(hv "$h")" != "${HT#v}" ]; then
    t=$(mktemp -d)
    if [ "$(get "components.podman.helpers.$h.build")" = source ]; then
      # Release binary needs a newer glibc than Debian 12: build the tag with its vendored crates and a pinned Rust.
      RA=$(get components.podman.build.rust.asset); RS=$(get components.podman.build.rust.sha256)
      VA=$(get "components.podman.helpers.$h.vendor"); VS=$(get "components.podman.helpers.$h.vendor_sha256")
      curl -fsSL -o "$t/$RA" "https://static.rust-lang.org/dist/$RA"; curl -fsSL -o "$t/$VA" "$HU/releases/download/$HT/$VA"
      printf '%s  %s\n%s  %s\n' "$RS" "$t/$RA" "$VS" "$t/$VA" | sha256sum -c --quiet || { rm -rf "$t"; echo "FAIL  $h build inputs checksum mismatch"; exit 1; }
      tar -xJf "$t/$RA" -C "$t"; "$t/${RA%.tar.xz}/install.sh" --prefix="$t/rust" --components=rustc,cargo,rust-std-x86_64-unknown-linux-gnu >/dev/null 2>&1
      git -c advice.detachedHead=false clone -q --depth 1 --branch "$HT" "$HU" "$t/src"
      [ "$(git -C "$t/src" rev-parse HEAD)" = "$(get "components.podman.helpers.$h.sha")" ] || { rm -rf "$t"; echo "FAIL  $h $HT is not the pinned commit"; exit 1; }
      tar -xzf "$t/$VA" -C "$t/src"
      mkdir -p "$t/src/.cargo"; printf '[source.crates-io]\nreplace-with = "vendored-sources"\n[source.vendored-sources]\ndirectory = "vendor"\n' > "$t/src/.cargo/config.toml"
      ( cd "$t/src" && PATH=$t/rust/bin:$PATH CARGO_HOME=$t/cargo cargo build -q --release --offline --bin "$h" >"$t/build.log" 2>&1 ) \
        || { tail -20 "$t/build.log"; rm -rf "$t"; echo "FAIL  $h build"; exit 1; }
      sudo install -D -m 0755 -o root -g root "$t/src/target/release/$h" "$PH/$h"; chg "$h $HT built from source"
    else
      HA=$(get "components.podman.helpers.$h.asset"); HS=$(get "components.podman.helpers.$h.sha256")
      curl -fsSL -o "$t/$HA" "$HU/releases/download/$HT/$HA"
      echo "$HS  $t/$HA" | sha256sum -c --quiet || { rm -rf "$t"; echo "FAIL  $h checksum mismatch"; exit 1; }
      case "$HA" in *.gz) gunzip "$t/$HA"; HA=${HA%.gz};; esac
      sudo install -D -m 0755 -o root -g root "$t/$HA" "$PH/$h"; chg "$h $HT installed"
    fi
    rm -rf "$t"
  fi
  [ "$(hv "$h")" = "${HT#v}" ] && ok "$h $HT" || bad "$h version"
done
# Rootless config and the socket units for the built binary (Debian's units left with its package).
mkdir -p "$HOME/.config/containers" "$HOME/.config/systemd/user"   # a fresh box has neither (11/17)
cat > "$HOME/.config/containers/containers.conf.new" <<C
# Generated by scripts/lid/stack_up.sh from stack/stack.yaml (ADR 26-11-03-002). Do not edit on the box.
[engine]
helper_binaries_dir = ["$PH", "/usr/libexec/podman", "/usr/lib/podman"]
runtime = "crun"
[engine.runtimes]
crun = ["$PH/crun"]
[network]
network_backend = "netavark"
default_rootless_network_cmd = "$(get components.podman.rootless_network_cmd)"
C
cat > "$HOME/.config/systemd/user/podman.socket.new" <<'U'
# Generated by scripts/lid/stack_up.sh (ADR 26-11-03-002). Rootless API socket for ToolHive; no /var/run/docker.sock.
[Unit]
Description=Podman API Socket
[Socket]
ListenStream=%t/podman/podman.sock
SocketMode=0660
[Install]
WantedBy=sockets.target
U
cat > "$HOME/.config/systemd/user/podman.service.new" <<U
# Generated by scripts/lid/stack_up.sh (ADR 26-11-03-002).
[Unit]
Description=Podman API Service
Requires=podman.socket
After=podman.socket
StartLimitIntervalSec=0
[Service]
Delegate=true
Type=exec
KillMode=process
Environment=LOGGING="--log-level=info"
ExecStart=$PB \$LOGGING system service
[Install]
WantedBy=default.target
U
pchg=; for f in containers/containers.conf systemd/user/podman.socket systemd/user/podman.service; do
  if cmp -s "$HOME/.config/$f.new" "$HOME/.config/$f"; then rm "$HOME/.config/$f.new"; else mv "$HOME/.config/$f.new" "$HOME/.config/$f"; chg "$f"; pchg=1; fi; done
systemctl --user daemon-reload
[ -n "$pchg" ] && { systemctl --user stop podman.service 2>/dev/null || true; podman system migrate >/dev/null 2>&1 || true; }
[ "$(podman info --format '{{.Host.NetworkBackend}}' 2>/dev/null)" = netavark ] && ok "podman network backend netavark ($(command -v podman))" || bad "podman network backend"
[ -e /var/run/docker.sock ] && bad "/var/run/docker.sock exists: Discobox's dormant Docker provider would find it" || ok "no host docker.sock"
systemctl --user is-enabled -q podman.socket 2>/dev/null || { systemctl --user enable -q podman.socket; chg "podman.socket enabled"; }
systemctl --user is-active -q podman.socket && [ -z "$pchg" ] || { systemctl --user restart podman.socket; }
systemctl --user is-active -q podman.socket && ok "podman.socket active" || bad "podman.socket not active"

# 2. thv, pinned by tag + sha256
TAG=$(get components.toolhive.pin.tag); ASSET=$(get components.toolhive.pin.asset); SUM=$(get components.toolhive.pin.sha256)
if [ "$(thv version 2>/dev/null | awk '/^ToolHive/{print $2}')" != "$TAG" ]; then
  t=$(mktemp -d); curl -fsSL -o "$t/$ASSET" "https://github.com/stacklok/toolhive/releases/download/$TAG/$ASSET"
  echo "$SUM  $t/$ASSET" | sha256sum -c --quiet || { rm -rf "$t"; echo "FAIL  thv checksum mismatch"; exit 1; }
  tar -xzf "$t/$ASSET" -C "$t" thv && install -m 0755 "$t/thv" "$HOME/.local/bin/thv"; rm -rf "$t"; chg "thv $TAG installed"
  thvnew=1   # running proxies still execute the old binary: section 5 restarts every thv unit
fi
[ "$(thv version | awk '/^ToolHive/{print $2}')" = "$TAG" ] && ok "thv $TAG ($(thv version | awk '/^Commit/{print $2}'))" || bad "thv version"
# 2b. uv, pinned by tag + sha256 (disco_fan.sh --deps resolves its wheelhouse with it; a fresh box has none: 11/18)
UT=$(get components.uv.pin.tag); UA=$(get components.uv.pin.asset); US=$(get components.uv.pin.sha256)
if [ "$("$HOME/.local/bin/uv" --version 2>/dev/null | awk '{print $2}')" != "$UT" ]; then
  t=$(mktemp -d); curl -fsSL -o "$t/$UA" "https://github.com/astral-sh/uv/releases/download/$UT/$UA"
  echo "$US  $t/$UA" | sha256sum -c --quiet || { rm -rf "$t"; echo "FAIL  uv checksum mismatch"; exit 1; }
  tar -xzf "$t/$UA" -C "$t" && install -m 0755 "$t"/*/uv "$HOME/.local/bin/uv" && install -m 0755 "$t"/*/uvx "$HOME/.local/bin/uvx"; rm -rf "$t"; chg "uv $UT installed"
fi
[ "$("$HOME/.local/bin/uv" --version 2>/dev/null | awk '{print $2}')" = "$UT" ] && ok "uv $UT" || bad "uv version"

# 3. encrypted secrets provider, non-interactive. thv keeps the store's password in an OS keyring; on a headless Linux host
# that is the kernel user keyring (keyctl), which does not survive a reboot. The master password lives in a 0600 file and
# thv_prime.sh re-primes the keyring before every thv unit starts. It never appears in argv, env, or output.
mkdir -p "$HOME/stack/bin" "$HOME/stack/profiles" "$HOME/.config/systemd/user"
install -d -m 0700 "$HOME/.config/epistemic-stack"
M=$HOME/.config/epistemic-stack/thv_master
[ -s "$M" ] || { (umask 077; head -c 32 /dev/urandom | base64 | tr -d '\n' > "$M"); chg "master password generated"; }
chmod 0600 "$M"
cat > "$HOME/stack/bin/thv_prime.sh" <<'P'
#!/bin/bash
# Put the ToolHive store password into the kernel user keyring (thv's keyctl fallback reads service "toolhive", key "toolhive").
keyctl padd user toolhive:toolhive @u < "$HOME/.config/epistemic-stack/thv_master" > /dev/null
P
chmod 0755 "$HOME/stack/bin/thv_prime.sh"; "$HOME/stack/bin/thv_prime.sh"
if ! grep -qs 'provider_type: encrypted' "$HOME/.config/toolhive/config.yaml" || ! grep -qs 'setup_completed: true' "$HOME/.config/toolhive/config.yaml"; then
  printf 'encrypted\n' | thv secret setup > /dev/null 2>&1 || bad "thv secret setup"; chg "secrets provider: encrypted"
fi
thv secret list >/dev/null 2>&1 && ok "secrets provider encrypted, store opens" || bad "secret store does not open"
# thv's anonymous usage metrics are on by default and the host's egress is open: off on our build box (11/17).
grep -q '^disable_usage_metrics: true' "$HOME/.config/toolhive/config.yaml" 2>/dev/null || { thv config usage-metrics disable >/dev/null 2>&1; chg "thv usage metrics off"; }
grep -q '^disable_usage_metrics: true' "$HOME/.config/toolhive/config.yaml" && ok "thv usage metrics off" || bad "thv usage metrics still on"

# 4. host firewall (system unit so it survives reboot). ADR 26-11-03-004: ToolHive proxies listen on loopback and only the
# gateway's own system user may open them; a sandbox (via Discobox's proxy or passt, both uid 1000) reaches the gateway port only.
PORTS=$(python3 -I - "$Y" <<'PY'
import sys, yaml
sys.path.insert(0, __import__('os').path.join(__import__('os').path.dirname(sys.argv[1]), '..', 'scripts', 'lid')); from stack_site import load; m = load(sys.argv[1]); t = [c for c in m['components'] if c['name'] == 'toolhive'][0]
print(','.join(str(s['port']) for s in t['servers']))
PY
)
GWP=$(get components.agentgateway.port); GWU=$(get components.agentgateway.user); TB=$(get components.toolhive.bind)
id -u "$GWU" >/dev/null 2>&1 || { sudo useradd --system --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin "$GWU"; chg "system user $GWU"; }
sudo tee /usr/local/sbin/epistemic_stack_fw.sh > /dev/null <<F
#!/bin/bash
# Generated by scripts/lid/stack_up.sh (ADRs 26-11-03-002, 26-11-03-004). Chain EPI_STACK:
#  - agentgateway ($IP:$GWP) takes connections only from the host's own address (Discobox's egress proxy runs here);
#  - uid $(id -u) (Discobox, passt, podman, this user's shells) may open no port on $IP except $GWP, and not the ToolHive
#    proxies ($PORTS on $TB) at all, so a sandbox reaches ToolHive only through the gateway;
#  - the gateway's user $GWU may open only the ToolHive proxies on $TB, nothing else, anywhere.
#  - uid $(id -u) never reaches the GCE metadata server (v4 or v6): no process of the box user needs instance credentials;
#    only the system user epi-wif mints one.
#  - uid $(id -u) on the loopback ADDRESSES (127/8, ::1) opens no system port (1-1023, so sshd and mail) but the DNS stub,
#    nor LLMNR (5355). Discobox's own ephemeral high-port listeners stay
#    reachable (source delivery uses them). Not 'the lo interface': traffic to the host's own $IP routes over lo too.
# Applied in ONE transaction (iptables-restore --noflush): the chains are never empty, so a reload opens no window.
set -e
U=$(id -u)
iptables-restore --noflush <<R
*filter
:EPI_STACK_INPUT - [0:0]
:EPI_STACK_OUTPUT - [0:0]
-A EPI_STACK_INPUT -p tcp --dport $GWP ! -s $IP -j DROP
-A EPI_STACK_INPUT -p tcp -m multiport --dports $PORTS ! -i lo -j DROP
-A EPI_STACK_OUTPUT -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN
-A EPI_STACK_OUTPUT -d 169.254.169.254/32 -p tcp ! --dport 53 -m owner --uid-owner \$U -j REJECT --reject-with tcp-reset
-A EPI_STACK_OUTPUT -d $IP -p tcp -m owner --uid-owner \$U ! --dport $GWP -j REJECT --reject-with tcp-reset
-A EPI_STACK_OUTPUT -d $IP -p udp -m owner --uid-owner \$U -j REJECT
-A EPI_STACK_OUTPUT -o lo -d 127.0.0.53/32 -p tcp --dport 53 -m owner --uid-owner \$U -j RETURN
-A EPI_STACK_OUTPUT -o lo -d 127.0.0.54/32 -p tcp --dport 53 -m owner --uid-owner \$U -j RETURN
-A EPI_STACK_OUTPUT -o lo -d $TB -p tcp -m multiport --dports $PORTS -m owner --uid-owner \$U -j REJECT --reject-with tcp-reset
-A EPI_STACK_OUTPUT -o lo -d 127.0.0.0/8 -p tcp -m multiport --dports 1:1023,5355 -m owner --uid-owner \$U -j REJECT --reject-with tcp-reset
-A EPI_STACK_OUTPUT -o lo -d 127.0.0.0/8 -p udp ! --dport 53 -m owner --uid-owner \$U -j REJECT
-A EPI_STACK_OUTPUT -o lo -d $TB -p tcp -m owner --uid-owner $GWU -m multiport --dports $PORTS -j RETURN
-A EPI_STACK_OUTPUT -p tcp -m owner --uid-owner $GWU -j REJECT --reject-with tcp-reset
-A EPI_STACK_OUTPUT -m owner --uid-owner $GWU -j REJECT
COMMIT
R
# IPv4 is in force before IPv6 is touched, and an IPv6 failure is loud but never undoes IPv4.
for c in INPUT OUTPUT; do iptables -C \$c -j EPI_STACK_\$c 2>/dev/null || iptables -I \$c 1 -j EPI_STACK_\$c; done
ip6tables-restore --noflush <<R6 || { logger -t epistemic-stack-fw "ip6tables-restore FAILED"; v6fail=1; }
*filter
:EPI_STACK_OUTPUT - [0:0]
-A EPI_STACK_OUTPUT -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN
-A EPI_STACK_OUTPUT -d fd20:ce::254/128 -p tcp ! --dport 53 -m owner --uid-owner \$U -j REJECT --reject-with tcp-reset
-A EPI_STACK_OUTPUT -o lo -d ::1/128 -p tcp -m multiport --dports 1:1023,5355,$PORTS -m owner --uid-owner \$U -j REJECT --reject-with tcp-reset
-A EPI_STACK_OUTPUT -o lo -d ::1/128 -p udp ! --dport 53 -m owner --uid-owner \$U -j REJECT
-A EPI_STACK_OUTPUT -m addrtype --dst-type LOCAL ! -d ::1/128 -p tcp -m owner --uid-owner \$U -j REJECT --reject-with tcp-reset
-A EPI_STACK_OUTPUT -m addrtype --dst-type LOCAL ! -d ::1/128 -m owner --uid-owner \$U -j REJECT
-A EPI_STACK_OUTPUT -m owner --uid-owner $GWU -j REJECT
COMMIT
R6
[ -n "\${v6fail:-}" ] || { ip6tables -C OUTPUT -j EPI_STACK_OUTPUT 2>/dev/null || ip6tables -I OUTPUT 1 -j EPI_STACK_OUTPUT; }
[ -z "\${v6fail:-}" ]
F
sudo chmod 0755 /usr/local/sbin/epistemic_stack_fw.sh
sudo tee /etc/systemd/system/epistemic-stack-fw.service > /dev/null <<'U'
[Unit]
Description=Epistemic stack firewall: gateway port host-only, ToolHive proxies gateway-only (ADRs 26-11-03-002, 26-11-03-004)
After=network-pre.target
Before=network.target systemd-user-sessions.service user@@UID@.service
[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/sbin/epistemic_stack_fw.sh
[Install]
WantedBy=multi-user.target
U
sudo sed -i "s/@UID@/$(id -u)/" /etc/systemd/system/epistemic-stack-fw.service
sudo systemctl daemon-reload; sudo systemctl enable -q epistemic-stack-fw.service; sudo systemctl restart epistemic-stack-fw.service
sudo iptables -S EPI_STACK_OUTPUT | grep -q -- "--uid-owner $(id -u "$GWU")" && sudo iptables -S EPI_STACK_INPUT | grep -q -- "--dport $GWP" \
  && sudo iptables -S EPI_STACK_OUTPUT | grep -q -- "-d 169.254.169.254/32 -p tcp -m tcp ! --dport 53 -m owner --uid-owner $(id -u)" \
  && ok "firewall EPI_STACK ($GWP host-only; $PORTS loopback, $GWU only)" || bad "firewall"

# 5. ToolHive server units
G=$(mktemp -d); python3 "$L/stack_units.py" "$Y" "$G" > "$G/servers.txt"
for f in "$G"/*.profile.json; do [ -e "$f" ] || continue; n=$(basename "$f" .profile.json)
  cmp -s "$f" "$HOME/stack/profiles/$n.json" || { install -m 0644 "$f" "$HOME/stack/profiles/$n.json"; chg "profile $n"; }; done
changed=; for f in "$G"/*.service; do u=$(basename "$f")
  cmp -s "$f" "$HOME/.config/systemd/user/$u" || { install -m 0644 "$f" "$HOME/.config/systemd/user/$u"; changed="$changed $u"; chg "unit $u"; }; done
systemctl --user daemon-reload
secrets=$(thv secret list 2>/dev/null || true)
while read -r n port unit secret; do
  if [ "$secret" != - ] && ! grep -qw -- "$secret" <<<"$secrets"; then
    systemctl --user disable -q --now "$unit" 2>/dev/null || true; echo "wait  $n: secret '$secret' not loaded (scripts/lid/stack_secret.sh $secret)"; continue; fi
  systemctl --user enable -q "$unit"
  if [[ " $changed " == *" $unit "* ]] || [ -n "${thvnew:-}" ] || ! systemctl --user is-active -q "$unit"; then systemctl --user restart "$unit"; fi
  up=; for i in $(seq 1 36); do sudo -n python3 "$L/mcp_probe.py" --direct "http://$TB:$port/mcp" list > "$G/$n.tools" 2>&1 && { up=1; break; }; sleep 5; done
  [ -n "$up" ] && ok "$n on $TB:$port, $(wc -l < "$G/$n.tools") tools" || { bad "$n not answering on $TB:$port: $(tail -1 "$G/$n.tools")"; journalctl --user -u "$unit" -n 8 --no-pager | sed 's/^/      /'; }
done < "$G/servers.txt"
rm -rf "$G"

# 5b. agentgateway (ADR 26-11-03-004): pinned binary, repo config, SESSION_KEY in a root-only env file, hardened system unit
# running as its own system user. One endpoint ($IP:$GWP/mcp) multiplexing the ToolHive servers on loopback.
AT=$(get components.agentgateway.pin.tag); AS=$(get components.agentgateway.pin.sha); AA=$(get components.agentgateway.pin.asset)
ASUM=$(get components.agentgateway.pin.sha256); ABIN=$(get components.agentgateway.install); AU=$(get components.agentgateway.unit)
AENV=$(get components.agentgateway.session_key); ACFG=/etc/epistemic-stack/agentgateway.yaml
if [ "$("$ABIN" --version 2>/dev/null | python3 -I -c 'import json,sys;print(json.load(sys.stdin)["git_revision"])' 2>/dev/null)" != "$AS" ]; then
  t=$(mktemp -d); curl -fsSL -o "$t/$AA" "https://github.com/agentgateway/agentgateway/releases/download/$AT/$AA"
  echo "$ASUM  $t/$AA" | sha256sum -c --quiet || { rm -rf "$t"; echo "FAIL  agentgateway checksum mismatch"; exit 1; }
  sudo install -m 0755 -o root -g root "$t/$AA" "$ABIN"; rm -rf "$t"; chg "agentgateway $AT installed"
fi
[ "$("$ABIN" --version | python3 -I -c 'import json,sys;print(json.load(sys.stdin)["git_revision"])')" = "$AS" ] && ok "agentgateway $AT ($AS)" || bad "agentgateway version"
# The repo config must agree with the manifest: bind address, port, and one loopback target per ToolHive server.
AGWC=$(mktemp); python3 "$L/stack_units.py" "$Y" render-agw "$R/stack/agentgateway/config.yaml" > "$AGWC"
python3 -I - "$Y" "$AGWC" <<'PY' || bad "stack/agentgateway/config.yaml disagrees with stack.yaml"
import sys, yaml
sys.path.insert(0, __import__('os').path.join(__import__('os').path.dirname(sys.argv[1]), '..', 'scripts', 'lid')); from stack_site import load; m = load(sys.argv[1]); c = yaml.safe_load(open(sys.argv[2]))
comp = {x['name']: x for x in m['components']}; a, t = comp['agentgateway'], comp['toolhive']
g = c['gateways']['default']; assert g['bindAddress'] == m['host']['internal_ip'] and g['port'] == a['port'], 'bind'
want = {s['name']: 'http://%s:%d/mcp' % (t['bind'], s['port']) for s in t['servers'] if s['name'] in a['targets']}
have = {x['name']: x['mcp']['host'] for x in c['mcp']['targets']}
assert have == want, (have, want)
assert 'session' not in (c.get('config') or {}), 'session key must not be in the config'
PY
sudo install -d -m 0755 -o root -g root /etc/epistemic-stack
"$ABIN" -f "$AGWC" --validate-only > /dev/null || bad "agentgateway --validate-only"
sudo cmp -s "$AGWC" "$ACFG" || { sudo install -m 0644 -o root -g root "$AGWC" "$ACFG"; chg "agentgateway config"; arestart=1; }
# SESSION_KEY encrypts MCP session ids. Generated once; never in argv, output or the config file.
sudo test -s "$AENV" || { sudo sh -c "umask 077; { printf 'SESSION_KEY='; head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n'; echo; } > '$AENV'"; chg "agentgateway session key generated"; arestart=1; }
sudo chown root:root "$AENV"; sudo chmod 0600 "$AENV"
sudo tee "/etc/systemd/system/$AU.new" > /dev/null <<U
# Generated by scripts/lid/stack_up.sh from stack/stack.yaml (ADR 26-11-03-004). Do not edit on the box.
[Unit]
Description=agentgateway $AT MCP gateway on $IP:$GWP (ADR 26-11-03-004)
After=network-online.target epistemic-stack-fw.service
Wants=network-online.target
Requires=epistemic-stack-fw.service
[Service]
User=$GWU
Group=$GWU
EnvironmentFile=$AENV
ExecStart=$ABIN -f $ACFG
RuntimeDirectory=epistemic-agw
RuntimeDirectoryMode=0750
Restart=on-failure
RestartSec=5
NoNewPrivileges=yes
CapabilityBoundingSet=
AmbientCapabilities=
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
PrivateDevices=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectKernelLogs=yes
ProtectControlGroups=yes
ProtectClock=yes
ProtectHostname=yes
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
RestrictNamespaces=yes
RestrictSUIDSGID=yes
LockPersonality=yes
SystemCallArchitectures=native
SystemCallFilter=@system-service
UMask=0077
[Install]
WantedBy=multi-user.target
U
if sudo cmp -s "/etc/systemd/system/$AU.new" "/etc/systemd/system/$AU"; then sudo rm "/etc/systemd/system/$AU.new"
else sudo mv "/etc/systemd/system/$AU.new" "/etc/systemd/system/$AU"; chg "unit $AU"; arestart=1; fi
sudo systemctl daemon-reload; sudo systemctl enable -q "$AU"
if [ -n "${arestart:-}" ] || ! systemctl is-active -q "$AU"; then sudo systemctl restart "$AU"; fi
up=; for i in $(seq 1 30); do sudo curl -s -o /dev/null -w '%{http_code}' --unix-socket /run/epistemic-agw/ready.sock http://x/healthz/ready 2>/dev/null | grep -q 200 && { up=1; break; }; sleep 2; done
GT=$(mktemp); python3 "$L/mcp_probe.py" --direct "http://$IP:$GWP/mcp" list > "$GT" 2>&1 || true
# `|| true`: under set -e + pipefail a target with no tools ended the whole run here, silently (fresh box, 11/17).
nt=$(for x in $(get components.agentgateway.targets); do grep -q "^${x}_" "$GT" && echo "$x"; done | tr '\n' ' ' || true)
if [ -n "$up" ] && [ "$nt" = "$(get components.agentgateway.targets) " ]; then ok "agentgateway on $IP:$GWP, $(wc -l < "$GT") tools from: $nt"
else bad "agentgateway not serving every target on $IP:$GWP (ready=${up:-no}; targets: ${nt:-none}): $(tail -1 "$GT")"; sudo journalctl -u "$AU" -n 8 --no-pager | sed 's/^/      /'; fi
rm -f "$GT"

# 6. what was already on the box, now under the manifest
cmp -s "$L/idle_shutdown.sh" /usr/local/sbin/idle_shutdown.sh || { sudo install -m 0755 "$L/idle_shutdown.sh" /usr/local/sbin/idle_shutdown.sh; chg "idle_shutdown.sh"; }
echo "*/5 * * * * root IDLE_BOX_USER=$(get host.user) /usr/local/sbin/idle_shutdown.sh" | sudo tee /etc/cron.d/idle_shutdown.new > /dev/null
sudo cmp -s /etc/cron.d/idle_shutdown.new /etc/cron.d/idle_shutdown && sudo rm /etc/cron.d/idle_shutdown.new || { sudo mv /etc/cron.d/idle_shutdown.new /etc/cron.d/idle_shutdown; chg "idle cron"; }
ok "idle shutdown cron"
CS=$(get components.discobox.claude_secret); mkdir -p "$HOME/.config/systemd/user"
for u in wif-refresh.service wif-refresh.timer; do sed "s/@CLAUDE_SECRET@/$CS/" "$L/$u" > "/tmp/$u.$$"; cmp -s "/tmp/$u.$$" "$HOME/.config/systemd/user/$u" || { install -m 0644 "/tmp/$u.$$" "$HOME/.config/systemd/user/$u"; chg "$u"; }; rm -f "/tmp/$u.$$"; done
systemctl --user daemon-reload; systemctl --user enable -q --now wif-refresh.timer
systemctl --user is-active -q wif-refresh.timer && ok "wif-refresh.timer" || bad "wif-refresh.timer"
# The orphaned-job sweep (job_sweep.py): a reboot or a killed worker leaves a job with no == done and live boxes.
for u in discobox-ensure.service discobox-ensure.timer; do cmp -s "$L/$u" "$HOME/.config/systemd/user/$u" || { install -m 0644 "$L/$u" "$HOME/.config/systemd/user/$u"; chg "$u"; }; done
systemctl --user daemon-reload; systemctl --user enable -q --now discobox-ensure.timer
systemctl --user is-active -q discobox-ensure.timer && ok "discobox-ensure.timer" || bad "discobox-ensure.timer"
for u in job-sweep.service job-sweep.timer; do cmp -s "$L/$u" "$HOME/.config/systemd/user/$u" || { install -m 0644 "$L/$u" "$HOME/.config/systemd/user/$u"; chg "$u"; }; done
systemctl --user daemon-reload; systemctl --user enable -q --now job-sweep.timer
systemctl --user is-active -q job-sweep.timer && ok "job-sweep.timer" || bad "job-sweep.timer"
DT=$(get components.discobox.pin.tag)
dv=$(discobox --auto-start-server=false version 2>&1 | awk '{print $NF}' | sort -u | tr '\n' ' '); [ "$dv" = "$DT " ] && ok "discobox $DT (client+server)" || bad "discobox versions: $dv (pin $DT)"
# ADR 26-11-10-001: Claude boxes run Discobox's own provisioning defaults, so the claude-code harness carries
# no egress override of ours; the lock is the credential route, exactly api.anthropic.com on the secret and its binding.
eg=$(discobox --auto-start-server=false admin harnesses get claude-code -o json | python3 -I -c 'import json,sys;e=json.load(sys.stdin).get("egress") or {};print(int(bool(e.get("denyByDefault") or e.get("allow"))))')
[ "$eg" = 0 ] || { discobox --auto-start-server=false admin harnesses update claude-code --egress-deny-by-default=false --clear-egress-allow >/dev/null && chg "claude-code harness egress -> Discobox default"; }
eg=$(discobox --auto-start-server=false admin harnesses get claude-code -o json | python3 -I -c 'import json,sys;e=json.load(sys.stdin).get("egress") or {};print(int(bool(e.get("denyByDefault") or e.get("allow"))))')
[ "$eg" = 0 ] && ok "claude-code harness egress: Discobox default" || bad "claude-code harness egress drifted from Discobox default"
rl=$(python3 "$L/route_lock.py" "$(get components.discobox.claude_secret)") && ok "claude credential: $rl" || bad "claude credential: $rl"
# Claude Code runs in a fresh Claude box (stack/GAPS.md G10): the pool provisions it once and reuses it, and a fresh pool
# once held an install with its native binary silently missing. --repair clears the pool's cache once, only when idle.
cr=$(bash "$L/claude_canary.sh" "$(get components.discobox.pool.name)" --repair 2>&1) && ok "$(tail -1 <<<"$cr")" || bad "claude canary: $(tr '\n' ' ' <<<"$cr")"
[ "$fail" = 0 ] && echo "stack_up: converged" || { echo "stack_up: NOT converged"; exit 1; }
