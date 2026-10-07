#!/bin/sh
# Install Yard on a VM. A second run repairs a partial install.
# A healthy Yard container stays in place. Data under /srv/yard stays in place.
# The image is ghcr.io/bobowski/yard:latest. --domain is the public host for Yard.
# Caddy listens on 8443. This script forwards 443 to that port.
set -eu

image="ghcr.io/bobowski/yard:latest"
domain=""
caddy_image="docker.io/library/caddy:2"
root="/srv/yard"
public_port="8000"
proxy_port="8443"

while [ $# -gt 0 ]; do
  case "$1" in
    --domain)
      [ $# -ge 2 ] || { echo "--domain needs a value" >&2; exit 2; }
      domain="$2"
      shift 2
      ;;
    *)
      echo "unknown arg: $1" >&2
      exit 2
      ;;
  esac
done

if ! command -v podman >/dev/null 2>&1 || ! command -v nft >/dev/null 2>&1; then
  if ! command -v apt-get >/dev/null 2>&1; then
    echo "podman and nft are required" >&2
    exit 1
  fi
  export DEBIAN_FRONTEND=noninteractive
  apt-get update
  apt-get install -y podman nftables git ca-certificates
fi
command -v podman >/dev/null 2>&1 || { echo "podman is required" >&2; exit 1; }
command -v nft >/dev/null 2>&1 || { echo "nft is required" >&2; exit 1; }

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
After=network-pre.target
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

run_yard() {
  sudo -u yard -- env HOME=/home/yard XDG_RUNTIME_DIR="/run/user/${uid}" "$@"
}

retry() {
  limit="$1"
  shift
  i=0
  while [ "$i" -lt "$limit" ]; do
    if "$@"; then
      return 0
    fi
    i=$((i + 1))
    sleep 1
  done
  return 1
}

i=0
while [ ! -d "/run/user/${uid}" ]; do
  i=$((i + 1))
  [ "$i" -le 30 ] || { echo "user runtime dir is not ready" >&2; exit 1; }
  sleep 1
done

run_yard systemctl --user enable --now podman.socket
sock="/run/user/${uid}/podman/podman.sock"
if ! retry 30 sh -c "test -S '$sock'"; then
  echo "podman socket $sock is not ready" >&2
  exit 1
fi
if ! retry 30 run_yard podman info >/dev/null 2>&1; then
  run_yard podman info >&2 || true
  echo "podman is not ready" >&2
  exit 1
fi

if ! run_yard podman network exists yard >/dev/null 2>&1; then
  run_yard podman network create yard >/dev/null
fi

pull_image() {
  if run_yard podman pull "$1"; then
    return 0
  fi
  if run_yard podman image exists "$1"; then
    echo "pull $1 failed. Using the image already on this host." >&2
    return 0
  fi
  echo "pull $1 failed" >&2
  return 1
}

pull_image "$image"
pull_image "$caddy_image"

caddy_sock="$root/run/caddy.sock"
autosave="$root/data/caddy/autosave.json"
if [ ! -s "$autosave" ]; then
  cat > "$autosave" <<EOF
{
	"admin": {
		"listen": "unix/${caddy_sock}|0600"
	},
	"apps": {
		"http": {
			"https_port": ${proxy_port},
			"servers": {
				"yard": {
					"automatic_https": {
						"disable_redirects": true
					},
					"listen": [
						":${proxy_port}"
					],
					"routes": []
				}
			}
		},
		"tls": {
			"automation": {
				"policies": [
					{
						"issuers": [
							{
								"challenges": {
									"http": {
										"disabled": true
									}
								},
								"module": "acme"
							}
						]
					}
				]
			}
		}
	}
}
EOF
fi
if [ -f "$autosave" ]; then
  chown yard:yard "$autosave"
  chmod 600 "$autosave"
fi

if [ -e "$caddy_sock" ] && [ ! -S "$caddy_sock" ]; then
  rm -f "$caddy_sock"
fi

run_caddy() {
  run_yard podman run -d --name caddy --restart=always --network yard \
    --log-driver journald \
    --entrypoint caddy \
    -p "${proxy_port}:${proxy_port}" \
    -v "$root/data/caddy:/config/caddy" \
    -v "$root/data/caddy/lib:/data" \
    -v "$root/run:$root/run" \
    "$caddy_image" \
    run --resume
}

if run_yard podman container exists caddy >/dev/null 2>&1; then
  running="$(run_yard podman inspect --format '{{.State.Running}}' caddy 2>/dev/null || true)"
  if [ "$running" != "true" ]; then
    run_yard podman start caddy >/dev/null 2>&1 || {
      run_yard podman rm -f caddy >/dev/null 2>&1 || true
      run_caddy
    }
  fi
else
  run_caddy
fi

if ! retry 30 sh -c "test -S '$caddy_sock'"; then
  run_yard podman rm -f caddy >/dev/null 2>&1 || true
  run_caddy
fi
if ! retry 30 sh -c "test -S '$caddy_sock'"; then
  echo "caddy socket $caddy_sock is not ready" >&2
  exit 1
fi

yard_env_ok() {
  got="$(run_yard podman inspect --format '{{range .Config.Env}}{{println .}}{{end}}' yard 2>/dev/null || true)"
  for line in \
    "CONTAINER_HOST=unix:///run/podman/podman.sock" \
    "YARD_ROOT=${root}" \
    "YARD_CADDY_SOCK=${caddy_sock}" \
    "YARD_PODMAN_SOCK=${sock}" \
    "YARD_PUBLIC_PORT=${public_port}" \
    "YARD_SELF_IMAGE=${image}" \
    "YARD_IMAGE=${image}" \
    "YARD_BOOT=1"
  do
    printf '%s\n' "$got" | grep -Fxq "$line" || return 1
  done
  if [ -n "$domain" ]; then
    printf '%s\n' "$got" | grep -Fxq "YARD_DOMAIN=${domain}" || return 1
  fi
}

yard_health() {
  run_yard podman exec yard /workspace/.venv/bin/python -c \
    'import urllib.request; urllib.request.urlopen("http://127.0.0.1:8000/health", timeout=3).read()' \
    >/dev/null 2>&1
}

run_yard_container() {
  set -- podman run -d --name yard --restart=always --network yard \
    --log-driver journald --group-add keep-groups \
    -p "127.0.0.1:${public_port}:${public_port}" \
    -v "${root}:${root}" \
    -v "${sock}:/run/podman/podman.sock" \
    -e CONTAINER_HOST=unix:///run/podman/podman.sock \
    -e "YARD_ROOT=${root}" \
    -e "YARD_PODMAN_SOCK=${sock}" \
    -e "YARD_CADDY_SOCK=${caddy_sock}" \
    -e "YARD_PUBLIC_PORT=${public_port}" \
    -e YARD_BOOT=1 \
    -e "YARD_SELF_IMAGE=${image}" \
    -e "YARD_IMAGE=${image}"
  if [ -n "$domain" ]; then
    set -- "$@" -e "YARD_DOMAIN=${domain}"
  fi
  set -- "$@" "$image"
  run_yard "$@"
}

if run_yard podman container exists yard >/dev/null 2>&1; then
  running="$(run_yard podman inspect --format '{{.State.Running}}' yard 2>/dev/null || true)"
  if [ "$running" != "true" ] && yard_env_ok; then
    run_yard podman start yard >/dev/null 2>&1 || true
    running="$(run_yard podman inspect --format '{{.State.Running}}' yard 2>/dev/null || true)"
  fi
  if [ "$running" != "true" ] || ! yard_env_ok || ! retry 30 yard_health; then
    run_yard podman rm -f yard >/dev/null 2>&1 || true
    run_yard_container
  fi
else
  run_yard_container
fi

if ! retry 60 yard_health; then
  echo "Yard did not answer on http://127.0.0.1:${public_port}/health" >&2
  exit 1
fi

token=""
i=0
while [ "$i" -lt 30 ]; do
  if [ -s "$root/data/yard/admin.token" ]; then
    token="$(cat "$root/data/yard/admin.token")"
    break
  fi
  i=$((i + 1))
  sleep 1
done
if [ -z "$token" ]; then
  echo "admin token was not written to $root/data/yard/admin.token" >&2
  exit 1
fi

if [ -f /etc/systemd/system/yard-tailscale.service ]; then
  systemctl disable --now yard-tailscale.service >/dev/null 2>&1 || true
  rm -f /etc/systemd/system/yard-tailscale.service
  systemctl daemon-reload
fi

echo "token: $token"
if [ -n "$domain" ]; then
  echo "url: https://$domain"
else
  echo "url: yard settings --domain <host>" >&2
fi
echo "local: http://127.0.0.1:${public_port}"
