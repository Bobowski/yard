#!/bin/sh
# Install Yard on a VM. A second run repairs a partial install.
# A healthy Yard container stays in place. Data under /srv/yard stays in place.
# The source is the public Yard repo. --domain is the public host for Yard.
# Caddy listens on 443 inside its container; the host publishes 8443 for TCP and UDP.
# This script forwards public 443, TCP and UDP, to that port.
set -eu

die() {
  echo "error: $1" >&2
  shift
  while [ $# -gt 0 ]; do
    echo "$1" >&2
    shift
  done
  exit 1
}

origin="https://github.com/Bobowski/yard.git"
domain=""
root="/srv/yard"
public_port="8000"
proxy_port="8443"

while [ $# -gt 0 ]; do
  case "$1" in
    --domain)
      [ $# -ge 2 ] || die "--domain needs a value" "Hint: ./install.sh --domain yard.example.com"
      domain="$2"
      shift 2
      ;;
    --remote)
      [ $# -ge 2 ] || die "--remote needs a git url" "Hint: ./install.sh --remote https://github.com/Bobowski/yard.git"
      origin="$2"
      shift 2
      ;;
    *)
      die "unknown arg: $1" "Hint: ./install.sh --domain <host>"
      ;;
  esac
done

[ "$(id -u)" -eq 0 ] || die "Run this script as root." \
  "It creates the yard user, writes /etc files, and loads an nft table." \
  "Hint: sudo ./install.sh --domain <host>"

if ! command -v nft >/dev/null 2>&1 || ! command -v sudo >/dev/null 2>&1 || ! command -v wget >/dev/null 2>&1 || ! command -v newuidmap >/dev/null 2>&1; then
  if ! command -v apt-get >/dev/null 2>&1; then
    die "nft, wget, sudo, and uidmap are required" \
      "The script installs them with apt-get when that command exists." \
      "Hint: install nftables, wget, sudo, and uidmap, then rerun this script."
  fi
  export DEBIAN_FRONTEND=noninteractive
  apt-get update
  # uidmap gives rootless Podman newuidmap/newgidmap; podman itself is the
  # pinned static build below, not the apt package.
  apt-get install -y nftables git ca-certificates wget sudo iproute2 procps uidmap
fi
command -v nft >/dev/null 2>&1 || die "nft is not installed" "Hint: apt-get install -y nftables"

# Pinned Podman. Ubuntu ships 4.9, whose compat API drops container labels on
# the list endpoint, so Compose recreates every container on every up. The
# static bundle carries the matched netavark/aardvark/pasta set Podman 6 needs.
# The first run migrates the BoltDB state to SQLite. An apt podman stays in
# place: the unit drop-in below picks which binary serves the socket.
podman_version="6.1.4"
podman_sha256="9dd51073fac5a1742f88cf3caff5554615cb4d67ba554d25e8807be635547af5"
if ! /usr/local/bin/podman version --format '{{.Client.Version}}' 2>/dev/null | grep -qx "$podman_version"; then
  tgz="$(mktemp)"
  wget -q -O "$tgz" \
    "https://github.com/mgoltzsche/podman-static/releases/download/v${podman_version}/podman-linux-amd64.tar.gz" \
    || die "Could not download podman-static ${podman_version}." \
      "Hint: curl -I https://github.com/mgoltzsche/podman-static/releases/tag/v${podman_version}"
  echo "${podman_sha256}  ${tgz}" | sha256sum -c - \
    || die "The podman-static checksum did not match." "Hint: the release may have changed; verify by hand."
  # Only usr/ is extracted. The bundle's etc/containers would set a file event
  # log and a fuse-overlayfs mount program; this host uses journald and native
  # overlay.
  tar -xzf "$tgz" --strip-components=1 -C / "podman-linux-amd64/usr"
  rm -f "$tgz"
fi

if ! id yard >/dev/null 2>&1; then
  useradd --create-home --shell /bin/bash yard
fi
add_subids() {
  file="$1"
  flag="$2"
  if [ -f "$file" ] && grep -q "^yard:" "$file"; then
    return 0
  fi
  start=100000
  if [ -f "$file" ]; then
    while IFS=: read -r _name begin count _; do
      if [ -z "$begin" ] || [ -z "$count" ]; then
        continue
      fi
      end=$((begin + count))
      if [ "$end" -gt "$start" ]; then
        start=$end
      fi
    done < "$file"
  fi
  usermod "$flag" "${start}-$((start + 65535))" yard
}

add_subids /etc/subuid --add-subuids
add_subids /etc/subgid --add-subgids

uid="$(id -u yard)"
loginctl enable-linger yard
systemctl start "user@${uid}"

ensure_dir() {
  install -d -o yard -g yard -m 755 "$1"
  chown yard:yard "$1"
  chmod 755 "$1"
}

ensure_dir "$root"
# Yard's container writes apps/<name>/repo as the yard user. A root-owned
# directory here would reject every deploy on a fresh host.
ensure_dir "$root/apps/yard"
ensure_dir "$root/apps/yard/caddy"
ensure_dir "$root/apps/yard/caddy/config"
ensure_dir "$root/apps/yard/caddy/data"
ensure_dir "$root/apps/yard/caddy/run"
install -d -o yard -g yard -m 700 "$root/apps/yard"

if systemctl list-unit-files caddy.service >/dev/null 2>&1; then
  systemctl disable --now caddy.service >/dev/null 2>&1 || true
fi

install -d /etc/yard
cat > /etc/yard/proxy.nft <<EOF
table inet yard {
  chain prerouting {
    type nat hook prerouting priority dstnat; policy accept;
    tcp dport 443 redirect to :${proxy_port}
    udp dport 443 redirect to :${proxy_port}
  }
}
EOF
nft_bin="$(command -v nft)"
cat > /etc/systemd/system/yard-proxy.service <<EOF
[Unit]
Description=Forward public port 443 (TCP and UDP) to Yard Caddy
After=network-pre.target nftables.service
Before=network.target

[Service]
Type=oneshot
ExecStart=/bin/sh -c '${nft_bin} delete table inet yard 2>/dev/null || true; ${nft_bin} -f /etc/yard/proxy.nft'
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable yard-proxy.service
systemctl restart yard-proxy.service
if ! nft list table inet yard 2>/dev/null | grep -q "tcp dport 443 redirect to :${proxy_port}" ||
  ! nft list table inet yard 2>/dev/null | grep -q "udp dport 443 redirect to :${proxy_port}"; then
  die "The redirect from port 443 to ${proxy_port} is not loaded." \
    "Caddy is published on ${proxy_port} for TCP and UDP. Public clients use port 443." \
    "Hint: nft -f /etc/yard/proxy.nft && systemctl status yard-proxy.service" \
    "Context: table inet yard is missing or has a different target."
fi

run_yard() {
  sudo -u yard -- env HOME=/home/yard XDG_RUNTIME_DIR="/run/user/${uid}" "$@"
}

retry() {
  limit="$1"
  shift
  try=0
  while [ "$try" -lt "$limit" ]; do
    if "$@"; then
      return 0
    fi
    try=$((try + 1))
    sleep 1
  done
  return 1
}

allow_redirect_port() {
  if ! command -v ufw >/dev/null 2>&1; then
    return 0
  fi
  status="$(ufw status 2>/dev/null || true)"
  printf '%s\n' "$status" | grep -q "Status: active" || return 0
  for proto in tcp udp; do
    if printf '%s\n' "$status" | grep -q "^${proxy_port}/${proto}"; then
      continue
    fi
    detail="$(ufw allow "${proxy_port}/${proto}" 2>&1)" || die "ufw could not allow port ${proxy_port}/${proto}." \
      "This script redirects public port 443 to ${proxy_port}." \
      "ufw sees the packet after that redirect, so a rule for 443 does not cover ${proxy_port}." \
      "Hint: ufw allow ${proxy_port}/${proto}" \
      "Context: ${detail}"
    ufw status | grep -q "^${proxy_port}/${proto}" || die "ufw still does not allow port ${proxy_port}/${proto}." \
      "The allow command returned, but the rule is not listed." \
      "Hint: ufw status" \
      "Context: public port 443 is redirected to ${proxy_port}."
  done
}

ensure_traverse() {
  dir="$1"
  while [ "$dir" != "/" ]; do
    if ! run_yard test -x "$dir"; then
      if ! command -v setfacl >/dev/null 2>&1; then
        die "User yard cannot enter ${dir}." \
          "Podman mounts ${root} from this path. A parent without execute blocks the mount." \
          "Hint: setfacl -m u:yard:--x ${dir}" \
          "Context: stat from user yard returns permission denied. Install the acl package if setfacl is missing."
      fi
      setfacl -m "u:yard:--x" "$dir" || die "setfacl could not grant user yard execute on ${dir}." \
        "Podman mounts ${root} from this path." \
        "Hint: setfacl -m u:yard:--x ${dir}" \
        "Context: user yard cannot search ${dir}."
      run_yard test -x "$dir" || die "User yard still cannot enter ${dir}." \
        "The ACL was written, but the directory is still closed." \
        "Hint: namei -l ${root}" \
        "Context: user yard is $(id -u yard)."
    fi
    dir=$(dirname "$dir")
  done
}

release_port() {
  holders="$(ss -H -ltnp "sport = :${public_port}" 2>/dev/null || true)"
  if [ -z "$holders" ]; then
    return 0
  fi
  if printf '%s\n' "$holders" | grep -q yardd; then
    pid="$(printf '%s\n' "$holders" | sed -n 's/.*pid=\([0-9][0-9]*\).*/\1/p' | head -n 1)"
    owner="$(ps -o user= -p "$pid" 2>/dev/null | tr -d ' ' || true)"
    if [ -z "$owner" ]; then
      die "Port ${public_port} is held by yardd, and the owner could not be read." \
        "The Yard container publishes 127.0.0.1:${public_port}." \
        "Hint: ss -ltnp sport = :${public_port}" \
        "Context: ${holders}"
    fi
    systemctl --user --machine "${owner}@" disable --now yardd.service || die "Could not stop yardd for user ${owner}." \
      "That service holds 127.0.0.1:${public_port}, so the Yard container cannot bind it." \
      "Hint: systemctl --user --machine ${owner}@ disable --now yardd.service" \
      "Context: ${holders}"
    try=0
    while ss -H -ltn "sport = :${public_port}" 2>/dev/null | grep -q .; do
      try=$((try + 1))
      [ "$try" -le 15 ] || die "Port ${public_port} is still in use after yardd stopped." \
        "The Yard container publishes 127.0.0.1:${public_port}." \
        "Hint: ss -ltnp sport = :${public_port}" \
        "Context: yardd was stopped for user ${owner}."
      sleep 1
    done
    return 0
  fi
  if run_yard podman container exists yard-app-1 >/dev/null 2>&1; then
    return 0
  fi
  die "Port ${public_port} is already in use." \
    "The Yard container publishes 127.0.0.1:${public_port}." \
    "Hint: ss -ltnp sport = :${public_port}" \
    "Context: ${holders}"
}

i=0
while [ ! -d "/run/user/${uid}" ]; do
  i=$((i + 1))
  [ "$i" -le 30 ] || die "The user runtime directory /run/user/${uid} did not appear." \
    "Rootless Podman needs that directory for user yard." \
    "Hint: loginctl enable-linger yard && systemctl start user@${uid}" \
    "Context: waited 30 seconds."
  sleep 1
done

# Container logs go to journald so they survive a container recreate.
# Persistent storage is what splits per-user journals for the yard user.
install -d /etc/systemd/journald.conf.d
journald_conf='[Journal]
Storage=persistent
SystemMaxUse=2G
RateLimitIntervalSec=30s
RateLimitBurst=20000'
if [ "$(cat /etc/systemd/journald.conf.d/yard.conf 2>/dev/null)" != "$journald_conf" ]; then
  printf '%s\n' "$journald_conf" > /etc/systemd/journald.conf.d/yard.conf
  mkdir -p /var/log/journal
  systemd-tmpfiles --create --prefix /var/log/journal
  systemctl restart systemd-journald
fi

# Rootless Podman ships container logs and engine events to journald; the Yard API reads them.
install -d -o yard -g yard /home/yard/.config /home/yard/.config/containers
containers_conf='[containers]
log_driver = "journald"

[engine]
events_logger = "journald"'
if [ "$(cat /home/yard/.config/containers/containers.conf 2>/dev/null)" != "$containers_conf" ]; then
  printf '%s\n' "$containers_conf" > /home/yard/.config/containers/containers.conf
  chown yard:yard /home/yard/.config/containers/containers.conf
  chmod 644 /home/yard/.config/containers/containers.conf
  run_yard systemctl --user stop podman.service >/dev/null 2>&1 || true
  run_yard systemctl --user restart podman.socket
fi

# The packaged user unit ExecStart is /usr/bin/podman; pin it to the static
# build. The restart keeps containers up — only the API service reloads.
install -d -o yard -g yard /home/yard/.config/systemd/user/podman.service.d
podman_dropin='[Service]
ExecStart=
ExecStart=/usr/local/bin/podman $LOGGING system service'
dropin_file="/home/yard/.config/systemd/user/podman.service.d/podman-static.conf"
if [ "$(cat "$dropin_file" 2>/dev/null)" != "$podman_dropin" ]; then
  printf '%s\n' "$podman_dropin" > "$dropin_file"
  chown yard:yard "$dropin_file"
  chmod 644 "$dropin_file"
fi
run_yard systemctl --user daemon-reload
run_yard systemctl --user stop podman.service >/dev/null 2>&1 || true
run_yard systemctl --user restart podman.socket

run_yard systemctl --user enable --now podman.socket
sock="/run/user/${uid}/podman/podman.sock"
if ! retry 30 sh -c "test -S '$sock'"; then
  die "The Podman socket ${sock} is not ready." \
    "Yard talks to Podman through that socket." \
    "Hint: systemctl --user --machine yard@ status podman.socket" \
    "Context: waited 30 seconds for the socket file."
fi
if ! retry 30 run_yard podman info >/dev/null 2>&1; then
  info="$(run_yard podman info 2>&1 || true)"
  die "Podman is not ready for user yard." \
    "Rootless Podman must answer before containers can start." \
    "Hint: sudo -u yard podman info" \
    "Context: ${info}"
fi
# `podman info` runs the local engine, not the socket service. The unit's
# effective ExecStart is what serves the socket, so assert it directly.
exec_start="$(run_yard systemctl --user show podman.service --property=ExecStart --value 2>/dev/null || true)"
case "$exec_start" in
  *"/usr/local/bin/podman"*) ;;
  *) die "podman.service does not run the pinned Podman ${podman_version}." \
    "The drop-in must set ExecStart to /usr/local/bin/podman." \
    "Hint: systemctl --user --machine yard@ cat podman.service" \
    "Context: ExecStart is ${exec_start:-empty}." ;;
esac
allow_redirect_port
ensure_traverse "$root"

if ! run_yard podman network exists yard >/dev/null 2>&1; then
  run_yard podman network create yard >/dev/null || die "Podman could not create the yard network." \
    "Caddy and Yard join this bridge so they can reach each other and the internet." \
    "Hint: sudo -u yard podman network ls" \
    "Context: network create failed for user yard."
fi

release_port

if [ -f /etc/systemd/system/yard-tailscale.service ]; then
  systemctl disable --now yard-tailscale.service >/dev/null 2>&1 || true
  rm -f /etc/systemd/system/yard-tailscale.service
  systemctl daemon-reload
fi

# The Yard clone and the image build stay in shell; Python does the first deploy.
# An existing clone is left as it is; the running Yard owns it from then on.
src="$root/apps/yard/repo"
if [ ! -d "$src/.git" ]; then
  run_yard git clone --depth 1 "$origin" "$src" || die "Could not clone ${origin}." \
    "The host clones Yard once. After the first deploy, pushes keep it moving." \
    "Hint: git ls-remote ${origin} HEAD" \
    "Context: clone failed for user yard."
fi
sha="$(run_yard git -C "$src" rev-parse HEAD)"
short="$(printf '%s' "$sha" | cut -c1-12)"

ensure_dir "$root/apps/yard/data"
ensure_dir "$root/tokens"

run_yard podman build -t localhost/yard-apply -f "$src/ops/apply/Containerfile" "$src/ops/apply" || die "Could not build the apply image." \
  "That image runs Compose. The build uses ${src}/ops/apply." \
  "Hint: sudo -u yard podman build -f ${src}/ops/apply/Containerfile ${src}/ops/apply" \
  "Context: the build failed for user yard."

# One apply container builds and starts Yard, the same way Yard deploys apps.
# The -e set matches what compose.apply passes for the yard app.
run_yard podman run --replace --name deploy-yard --network yard \
  -v "$sock:/run/podman/podman.sock" -v "$root:$root" \
  -e COMPOSE_PROJECT_NAME=yard -e COMPOSE_FILE="$src/ops/compose.yml" \
  -e APP_ENV="$root/apps/yard/env" -e CHECKOUT="$src" -e DATA_DIR="$root/apps/yard/data" \
  -e IMAGE="localhost/yard:$short" -e SHA="$sha" -e PROJECT_NAME=yard \
  -e YARD_ROOT="$root" -e YARD_PODMAN_SOCK="$sock" \
  localhost/yard-apply up || die "The first deploy failed." "Hint: podman logs deploy-yard"

i=0
while [ ! -s "$root/tokens/admin.token" ] && [ "$i" -lt 60 ]; do
  i=$((i + 1))
  sleep 1
done
[ -s "$root/tokens/admin.token" ] || die "Yard did not write its admin token." \
  "Hint: podman logs yard-app-1" \
  "Context: the file was missing or empty after 60 seconds."
token="$(cat "$root/tokens/admin.token")"
echo "token: $token"
if [ -n "$domain" ]; then
  echo "url: https://$domain"
else
  echo "url: pass --domain to publish this host"
fi
echo "local: http://127.0.0.1:8000"

# Setup runs on the laptop, through the yard CLI.
echo ""
echo "Next steps:"
echo "  yard login --url https://${domain:-<domain>} --token <token>"
echo "  yard forge setup origin    # or: yard forge setup github"
echo "  yard app set --app yard --repo <host>/<owner>/yard --trigger push"
