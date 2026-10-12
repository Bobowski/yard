#!/bin/sh
# Install Yard on a VM. A second run repairs a partial install.
# A healthy Yard container stays in place. Data under /srv/yard stays in place.
# The source is the public Yard repo. --domain is the public host for Yard.
# Caddy listens on 8443. This script forwards 443 to that port.
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

if ! command -v podman >/dev/null 2>&1 || ! command -v nft >/dev/null 2>&1; then
  if ! command -v apt-get >/dev/null 2>&1; then
    die "podman and nft are required" \
      "The script installs them with apt-get when that command exists." \
      "Hint: install podman and nftables, then rerun this script."
  fi
  export DEBIAN_FRONTEND=noninteractive
  apt-get update
  apt-get install -y podman nftables git ca-certificates wget
fi
command -v podman >/dev/null 2>&1 || die "podman is not installed" "Hint: apt-get install -y podman"
command -v nft >/dev/null 2>&1 || die "nft is not installed" "Hint: apt-get install -y nftables"

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
ensure_dir "$root/data"
ensure_dir "$root/data/yard"
ensure_dir "$root/data/caddy"
ensure_dir "$root/data/caddy/lib"
ensure_dir "$root/repos"
ensure_dir "$root/run"
# The slot env files. Yard writes them with mode 600; the directory stays closed too.
install -d -o yard -g yard -m 700 "$root/env"
# The slot env file keeps whatever the operator added; only the domain line is managed here.
if [ -n "$domain" ]; then
  touch "$root/env/yard.env"
  grep -v '^YARD_DOMAIN=' "$root/env/yard.env" > "$root/env/yard.env.tmp" || true
  printf 'YARD_DOMAIN=%s\n' "$domain" >> "$root/env/yard.env.tmp"
  mv "$root/env/yard.env.tmp" "$root/env/yard.env"
  chown yard:yard "$root/env/yard.env"
  chmod 600 "$root/env/yard.env"
fi

if systemctl list-unit-files caddy.service >/dev/null 2>&1; then
  systemctl disable --now caddy.service >/dev/null 2>&1 || true
fi

install -d /etc/yard
cat > /etc/yard/proxy.nft <<EOF
table inet yard {
  chain prerouting {
    type nat hook prerouting priority dstnat; policy accept;
    tcp dport 443 redirect to :${proxy_port}
  }
}
EOF
nft_bin="$(command -v nft)"
cat > /etc/systemd/system/yard-proxy.service <<EOF
[Unit]
Description=Forward public port 443 to Yard Caddy
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
if ! nft list table inet yard 2>/dev/null | grep -q "tcp dport 443 redirect to :${proxy_port}"; then
  die "The redirect from port 443 to ${proxy_port} is not loaded." \
    "Caddy listens on ${proxy_port}. Public clients use port 443." \
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
  if printf '%s\n' "$status" | grep -q "^${proxy_port}/tcp"; then
    return 0
  fi
  detail="$(ufw allow "${proxy_port}/tcp" 2>&1)" || die "ufw could not allow port ${proxy_port}." \
    "This script redirects public port 443 to ${proxy_port}." \
    "ufw sees the packet after that redirect, so a rule for 443 does not cover ${proxy_port}." \
    "Hint: ufw allow ${proxy_port}/tcp" \
    "Context: ${detail}"
  ufw status | grep -q "^${proxy_port}/tcp" || die "ufw still does not allow port ${proxy_port}." \
    "The allow command returned, but the rule is not listed." \
    "Hint: ufw status" \
    "Context: public port 443 is redirected to ${proxy_port}."
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
  if run_yard podman container exists yard >/dev/null 2>&1; then
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

# The bare repo and the image build stay in shell; Python does the first deploy.
repo="$root/repos/yard.git"
if [ ! -d "$repo" ]; then
  run_yard git clone --bare "$origin" "$repo" || die "Could not clone ${origin}." \
    "The host keeps that bare repo and builds Yard from it." \
    "Hint: git ls-remote ${origin} HEAD" \
    "Context: clone failed for user yard."
  run_yard git -C "$repo" config remote.origin.fetch '+refs/heads/*:refs/remotes/origin/*'
  run_yard git -C "$repo" config uploadpack.allowFilter true
  run_yard git -C "$repo" remote set-head origin -a || true
fi
run_yard git -C "$repo" fetch origin || die "Could not fetch origin." \
  "The install follows main. Fetch fast-forwards main from origin." \
  "Hint: git -C ${repo} remote -v" \
  "Context: fetch failed for user yard."
sha="$(run_yard git -C "$repo" rev-parse refs/remotes/origin/main)"

src="$root/checkouts/yard/$sha"
if [ ! -d "$src" ]; then
  mkdir -p "$src.tmp"
  run_yard git -C "$repo" archive "$sha" | tar -x -C "$src.tmp" || die "Could not extract ${sha}." \
    "The checkout is ${src}." \
    "Hint: git -C ${repo} archive ${sha}" \
    "Context: archive failed for user yard."
  mv "$src.tmp" "$src"
  chown -R yard:yard "$root/checkouts/yard"
fi

ensure_dir "$root/data/yard"
ensure_dir "$root/data/caddy"
ensure_dir "$root/data/caddy/lib"

run_yard podman build -t localhost/yard-apply -f "$src/ops/apply/Containerfile" "$src/ops/apply" || die "Could not build the apply image." \
  "That image runs Compose. The build uses ${src}/ops/apply." \
  "Hint: sudo -u yard podman build -f ${src}/ops/apply/Containerfile ${src}/ops/apply" \
  "Context: the build failed for user yard."

# One apply container builds and starts Yard, the same way Yard deploys slots.
# The -e set matches what compose.apply passes for the yard slot.
run_yard podman run --replace --name yard-apply-yard --network yard \
  -v "$sock:/run/podman/podman.sock" -v "$root:$root" \
  -e COMPOSE_PROJECT_NAME=yard -e COMPOSE_FILE="$src/ops/compose.yml" -e PREVIOUS= \
  -e APP_ENV="$root/env/yard.env" -e CHECKOUT="$src" -e DATA_DIR="$root/data/yard" \
  -e IMAGE="localhost/yard:$sha" -e SHA="$sha" -e SLOT_NAME=yard \
  -e YARD_ROOT="$root" -e YARD_PODMAN_SOCK="$sock" \
  localhost/yard-apply up || die "The first deploy failed." "Hint: podman logs yard-apply-yard"

i=0
while [ ! -s "$root/data/yard/admin.token" ] && [ "$i" -lt 60 ]; do
  i=$((i + 1))
  sleep 1
done
[ -s "$root/data/yard/admin.token" ] || die "Yard did not write its admin token." \
  "Hint: podman logs yard" \
  "Context: the file was missing or empty after 60 seconds."
token="$(cat "$root/data/yard/admin.token")"
echo "token: $token"
if [ -n "$domain" ]; then
  echo "url: https://$domain"
else
  echo "url: pass --domain to publish this host"
fi
echo "local: http://127.0.0.1:8000"
run_yard podman exec -e YARD_URL=http://127.0.0.1:8000 -e YARD_TOKEN="$token" yard /workspace/.venv/bin/yard doctor || true
