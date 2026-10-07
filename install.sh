#!/bin/sh
# Install Yard on a VM. A second run repairs a partial install.
# A healthy Yard container stays in place. Data under /srv/yard stays in place.
# The image is ghcr.io/bobowski/yard:latest. --domain is the public host for Yard.
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

image="ghcr.io/bobowski/yard:latest"
domain=""
caddy_image="docker.io/library/caddy:2"
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
  apt-get install -y podman nftables git ca-certificates
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

route_ok() {
  name="$1"
  pid="$(run_yard podman inspect --format '{{.State.Pid}}' "$name" 2>/dev/null || true)"
  if [ -z "$pid" ] || [ "$pid" = "0" ]; then
    return 1
  fi
  addr="$(nsenter -t "$pid" -n ip -4 addr show scope global 2>/dev/null || true)"
  route="$(nsenter -t "$pid" -n ip -4 route show default 2>/dev/null || true)"
  [ -n "$addr" ] && [ -n "$route" ]
}

explain_route() {
  name="$1"
  pid="$(run_yard podman inspect --format '{{.State.Pid}}' "$name" 2>/dev/null || true)"
  addr="$(nsenter -t "$pid" -n ip -4 addr 2>/dev/null || true)"
  route="$(nsenter -t "$pid" -n ip -4 route 2>/dev/null || true)"
  die "Container ${name} has no path to the internet." \
    "The yard network is a bridge. A healthy container has an address and a default route." \
    "Podman can record an address while the namespace still has only loopback." \
    "Hint: podman rm -f caddy yard && podman network rm -f yard && rerun this script." \
    "Context: addresses [${addr:-none}] routes [${route:-none}]."
}

caddy_failed() {
  logs="$(run_yard podman logs --tail 30 caddy 2>&1 || true)"
  die "Caddy did not start." \
    "The container must mount ${root} and publish port ${proxy_port}." \
    "Hint: podman logs caddy" \
    "Context: user yard must be able to enter every parent of ${root}. ${logs}"
}

yard_failed() {
  logs="$(run_yard podman logs --tail 30 yard 2>&1 || true)"
  die "The Yard container did not start." \
    "It publishes 127.0.0.1:${public_port} and joins the yard network." \
    "Hint: ss -ltnp sport = :${public_port}" \
    "Context: ${logs}"
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

pull_image() {
  if run_yard podman pull "$1"; then
    return 0
  fi
  if run_yard podman image exists "$1"; then
    echo "pull $1 failed. Using the image already on this host." >&2
    return 0
  fi
  die "Could not pull ${1}." \
    "The host has no local copy of that image either." \
    "Hint: podman login ghcr.io when the package is private." \
    "Context: podman pull failed for user yard."
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
      run_caddy || caddy_failed
    }
  fi
else
  run_caddy || caddy_failed
fi

if ! retry 30 sh -c "test -S '$caddy_sock'"; then
  run_yard podman rm -f caddy >/dev/null 2>&1 || true
  run_caddy || caddy_failed
fi
if ! retry 30 sh -c "test -S '$caddy_sock'"; then
  logs="$(run_yard podman logs --tail 30 caddy 2>&1 || true)"
  die "Caddy did not create ${caddy_sock}." \
    "Yard loads the proxy config through that socket." \
    "Hint: podman logs caddy" \
    "Context: ${logs}"
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
    --log-driver journald \
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

release_port

if run_yard podman container exists yard >/dev/null 2>&1; then
  running="$(run_yard podman inspect --format '{{.State.Running}}' yard 2>/dev/null || true)"
  if [ "$running" != "true" ] && yard_env_ok; then
    run_yard podman start yard >/dev/null 2>&1 || true
    running="$(run_yard podman inspect --format '{{.State.Running}}' yard 2>/dev/null || true)"
  fi
  if [ "$running" != "true" ] || ! yard_env_ok || ! retry 30 yard_health; then
    run_yard podman rm -f yard >/dev/null 2>&1 || true
    run_yard_container || yard_failed
  fi
else
  run_yard_container || yard_failed
fi

if ! retry 60 yard_health; then
  logs="$(run_yard podman logs --tail 30 yard 2>&1 || true)"
  die "Yard did not answer on http://127.0.0.1:${public_port}/health." \
    "The API must answer before this script can treat the install as finished." \
    "Hint: podman logs yard" \
    "Context: ${logs}"
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
  die "The admin token was not written to ${root}/data/yard/admin.token." \
    "Yard writes that file on first boot when user yard can write the data directory." \
    "Hint: ls -l ${root}/data/yard" \
    "Context: the file was missing or empty after 30 seconds."
fi

if [ -f /etc/systemd/system/yard-tailscale.service ]; then
  systemctl disable --now yard-tailscale.service >/dev/null 2>&1 || true
  rm -f /etc/systemd/system/yard-tailscale.service
  systemctl daemon-reload
fi

command -v nsenter >/dev/null 2>&1 || die "nsenter is not installed." \
  "The script uses it to read the container route." \
  "Hint: apt-get install -y util-linux"
command -v ip >/dev/null 2>&1 || die "ip is not installed." \
  "The script uses it to read the container route." \
  "Hint: apt-get install -y iproute2"

for name in caddy yard; do
  if ! retry 15 route_ok "$name"; then
    explain_route "$name"
  fi
done

yard_reaches_caddy() {
  run_yard podman exec yard /workspace/.venv/bin/python -c \
    'import socket; socket.create_connection(("caddy", 8443), 5).close()' \
    >/dev/null 2>&1
}
if ! retry 20 yard_reaches_caddy; then
  detail="$(run_yard podman exec yard /workspace/.venv/bin/python -c \
    'import socket; socket.create_connection(("caddy", 8443), 5).close()' 2>&1 || true)"
  die "Yard cannot open a connection to caddy:8443." \
    "Both containers share the yard network. The name caddy is the DNS name on that bridge." \
    "Hint: podman network inspect yard" \
    "Context: ${detail}"
fi

yard_reaches_acme() {
  run_yard podman exec yard /workspace/.venv/bin/python -c \
    'import urllib.request; urllib.request.urlopen("https://acme-v02.api.letsencrypt.org/directory", timeout=15).read(32)' \
    >/dev/null 2>&1
}
if ! retry 20 yard_reaches_acme; then
  detail="$(run_yard podman exec yard /workspace/.venv/bin/python -c \
    'import urllib.request; urllib.request.urlopen("https://acme-v02.api.letsencrypt.org/directory", timeout=15).read(32)' 2>&1 || true)"
  die "The yard container cannot reach Let's Encrypt." \
    "Caddy uses that same network to register an account and request a certificate." \
    "Hint: podman rm -f caddy yard && podman network rm -f yard && rerun this script." \
    "Context: ${detail}"
fi

if [ -n "$domain" ]; then
  addrs="$(getent ahostsv4 "$domain" 2>/dev/null | awk '{print $1}' | sort -u || true)"
  mine="$(hostname -I 2>/dev/null || true)"
  dns_ok=0
  for addr in $addrs; do
    for one in $mine; do
      if [ "$addr" = "$one" ]; then
        dns_ok=1
      fi
    done
  done
  if [ "$dns_ok" -ne 1 ]; then
    die "DNS for ${domain} does not point at this host." \
      "Let's Encrypt connects to the address in that record." \
      "Hint: dig +short ${domain}" \
      "Context: DNS [${addrs:-none}] host addresses [${mine:-none}]."
  fi

  cert_path="${root}/data/caddy/lib/caddy/certificates/acme-v02.api.letsencrypt.org-directory/${domain}/${domain}.crt"
  cert_ready() {
    [ -s "$cert_path" ]
  }
  if ! retry 30 cert_ready; then
    run_yard podman restart caddy >/dev/null || die "Caddy did not restart for a new certificate attempt." \
      "The certificate for ${domain} was not on disk." \
      "Hint: podman logs caddy" \
      "Context: podman restart caddy failed."
    if ! retry 30 sh -c "test -S '$caddy_sock'"; then
      die "Caddy did not recreate ${caddy_sock} after a restart." \
        "The restart was a new certificate attempt for ${domain}." \
        "Hint: podman logs caddy" \
        "Context: waited 30 seconds for the socket."
    fi
    if ! retry 75 cert_ready; then
      detail="$(run_yard podman logs --tail 40 caddy 2>&1 | grep -E 'challenge failed|Timeout|firewall|certificate obtained' | tail -n 8 || true)"
      die "Caddy did not obtain a certificate for ${domain}." \
        "Let's Encrypt connects to port 443. This host redirects that port to ${proxy_port}." \
        "The firewall must allow ${proxy_port}. A cloud firewall must allow 443." \
        "Hint: ufw status && dig +short ${domain}" \
        "Context: ${detail:-no certificate error in the Caddy log}"
    fi
  fi

  https_ok() {
    if command -v curl >/dev/null 2>&1; then
      curl -fsS --max-time 15 --resolve "${domain}:${proxy_port}:127.0.0.1" \
        "https://${domain}:${proxy_port}/health" >/dev/null 2>&1 || return 1
      return 0
    fi
    YARD_CHECK_DOMAIN="$domain" YARD_CHECK_PORT="$proxy_port" python3 - <<'PY' || return 1
import os, socket, ssl
host = os.environ["YARD_CHECK_DOMAIN"]
port = int(os.environ["YARD_CHECK_PORT"])
raw = socket.create_connection(("127.0.0.1", port), 15)
tls = ssl.create_default_context().wrap_socket(raw, server_hostname=host)
request = "GET /health HTTP/1.1\r\nHost: %s\r\nConnection: close\r\n\r\n" % host
tls.sendall(request.encode())
data = b""
while True:
    chunk = tls.recv(4096)
    if not chunk:
        break
    data += chunk
status = data.split(b"\r\n", 1)[0]
if b" 200 " not in status:
    raise SystemExit(status.decode(errors="replace"))
PY
  }
  if ! https_ok; then
    die "https://${domain}:${proxy_port}/health failed on this host." \
      "The certificate file exists. Caddy did not serve a trusted response for that host." \
      "Hint: curl -v --resolve ${domain}:${proxy_port}:127.0.0.1 https://${domain}:${proxy_port}/health" \
      "Context: the check uses port ${proxy_port} on 127.0.0.1 with the public host name."
  fi

  settings_ok() {
    YARD_CHECK_TOKEN="$token" YARD_CHECK_DOMAIN="$domain" YARD_CHECK_PORT="$public_port" python3 - <<'PY' || return 1
import json, os, urllib.request
port = os.environ["YARD_CHECK_PORT"]
req = urllib.request.Request(
    "http://127.0.0.1:%s/api/v1/settings" % port,
    headers={"Authorization": "Bearer " + os.environ["YARD_CHECK_TOKEN"]},
)
with urllib.request.urlopen(req, timeout=5) as res:
    body = json.load(res)
if body.get("domain") != os.environ["YARD_CHECK_DOMAIN"]:
    raise SystemExit(str(body.get("domain")))
PY
  }
  if ! settings_ok; then
    die "Yard did not store the domain ${domain}." \
      "The public host is the settings domain. Caddy uses that value for the route." \
      "Hint: curl -H \"Authorization: Bearer <token>\" http://127.0.0.1:${public_port}/api/v1/settings" \
      "Context: GET /api/v1/settings returned a different domain."
  fi
fi

echo "token: $token"
if [ -n "$domain" ]; then
  echo "url: https://$domain"
  echo "checks: health, route, acme, certificate"
else
  echo "url: yard settings --domain <host>" >&2
  echo "checks: health, route, acme"
fi
echo "local: http://127.0.0.1:${public_port}"
