#!/bin/bash
# ASF Bridge + 导航页部署脚本（在服务器上以 root 运行）
set -euo pipefail

STAGING=/opt/asf-bridge-staging
DEST=/opt/asf-bridge
CONFIG="$DEST/config.json"

ASF_BRIDGE_CONFIG="$STAGING/config.json" python3 "$STAGING/asf-bridge.py" --check-config

echo "== 1/5 安装文件 =="
mkdir -p "$DEST"
if [[ ! -f "$STAGING/config.json" ]]; then
  echo "ERROR: bridge config not found in staging: $STAGING/config.json" >&2
  exit 1
fi
cp -f "$STAGING"/asf-bridge.py "$STAGING"/config.json "$STAGING"/nav-asf-snippet.html \
      "$STAGING"/inject_nav.py "$STAGING"/inject_nginx.py "$DEST"/
cp -f "$STAGING"/asf-bridge.service /etc/systemd/system/asf-bridge.service
chmod 600 "$CONFIG"
chmod 755 "$DEST"/asf-bridge.py

PORT=$(python3 - "$CONFIG" <<'PY'
import json, sys
with open(sys.argv[1], encoding="utf-8") as stream:
    value = json.load(stream).get("listen_port", 17001)
if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 65535:
    raise SystemExit("config listen_port must be an integer from 1 to 65535")
print(value)
PY
)

TOKEN=$(python3 - "$CONFIG" <<'PY'
import json, sys
with open(sys.argv[1], encoding="utf-8") as stream:
    value = json.load(stream).get("ui_token", "")
if not isinstance(value, str) or not value:
    raise SystemExit("config ui_token is empty")
print(value)
PY
)

normalize_domain() {
  python3 - "$1" <<'PY'
import sys
from urllib.parse import urlsplit
value = sys.argv[1].strip()
parsed = urlsplit(value if "://" in value else "//" + value)
print((parsed.hostname or "").rstrip(".").lower())
PY
}

DOMAIN_ARGS=()
PUBLIC_DOMAIN=""
if [[ -n "${ASF_NAV_DOMAIN:-}" ]]; then
  IFS=',' read -r -a requested_domains <<< "$ASF_NAV_DOMAIN"
  for requested in "${requested_domains[@]}"; do
    host=$(normalize_domain "$requested")
    if [[ -z "$host" || "$host" == *YOUR_DOMAIN* || "$host" == "localhost" ]]; then
      echo "ERROR: ASF_NAV_DOMAIN contains an invalid or placeholder hostname: $requested" >&2
      exit 1
    fi
    DOMAIN_ARGS+=(--domain "$host")
    [[ -n "$PUBLIC_DOMAIN" ]] || PUBLIC_DOMAIN="$host"
  done
else
  while IFS= read -r host; do
    [[ -n "$host" ]] || continue
    DOMAIN_ARGS+=(--domain "$host")
    [[ -n "$PUBLIC_DOMAIN" ]] || PUBLIC_DOMAIN="$host"
  done < <(python3 - "$CONFIG" <<'PY'
import ipaddress, json, sys
from urllib.parse import urlsplit
with open(sys.argv[1], encoding="utf-8") as stream:
    origins = json.load(stream).get("allowed_origins", [])
if not isinstance(origins, list):
    raise SystemExit("config allowed_origins must be a list")
seen = set()
for origin in origins:
    if not isinstance(origin, str):
        continue
    parsed = urlsplit(origin if "://" in origin else "//" + origin)
    host = (parsed.hostname or "").rstrip(".").lower()
    if not host or "your_domain" in host or host == "localhost":
        continue
    try:
        if ipaddress.ip_address(host).is_loopback:
            continue
    except ValueError:
        pass
    if host not in seen:
        seen.add(host)
        print(host)
PY
  )
fi

if [[ -z "$PUBLIC_DOMAIN" ]]; then
  echo "ERROR: no public domain found; set ASF_NAV_DOMAIN or configure a real hostname in allowed_origins" >&2
  exit 1
fi

NGINX_CONF="${ASF_NGINX_CONF:-/etc/nginx/sites-available/YOUR_SITE}"
if [[ "$NGINX_CONF" == *YOUR_SITE* ]]; then
  echo "ERROR: Nginx config still uses the YOUR_SITE template; set ASF_NGINX_CONF" >&2
  exit 1
fi
if [[ ! -f "$NGINX_CONF" ]]; then
  echo "ERROR: Nginx config not found: $NGINX_CONF" >&2
  exit 1
fi

echo "== 2/5 注入 Nginx 配置 =="
ROLLBACK_BACKUP=$(mktemp "${NGINX_CONF}.rollback.XXXXXX")
cp -p "$NGINX_CONF" "$ROLLBACK_BACKUP"
NGINX_ACTIVATED=0
rollback_on_failure() {
  result=$?
  if [[ "$result" -ne 0 && -f "$ROLLBACK_BACKUP" ]]; then
    cp -p "$ROLLBACK_BACKUP" "$NGINX_CONF"
    if [[ "$NGINX_ACTIVATED" == 1 ]]; then
      nginx -t && systemctl reload nginx || echo "ERROR: failed to reload restored Nginx config" >&2
    fi
    rm -f "$ROLLBACK_BACKUP"
    echo "ERROR: deployment failed; restored the pre-deploy Nginx config" >&2
  fi
  return "$result"
}
trap rollback_on_failure EXIT
restore_nginx_config() {
  cp -p "$ROLLBACK_BACKUP" "$NGINX_CONF"
  rm -f "$ROLLBACK_BACKUP"
}
if ! python3 "$DEST"/inject_nginx.py "$NGINX_CONF" --port "$PORT" "${DOMAIN_ARGS[@]}"; then
  restore_nginx_config
  echo "ERROR: Nginx injector failed; restored the pre-deploy config" >&2
  exit 1
fi
if ! nginx -t; then
  restore_nginx_config
  echo "ERROR: nginx -t failed; restored the pre-deploy config" >&2
  exit 1
fi
if ! systemctl reload nginx; then
  restore_nginx_config
  echo "ERROR: Nginx reload failed; restored the pre-deploy config" >&2
  exit 1
fi
NGINX_ACTIVATED=1

echo "== 3/5 注入导航页 =="
python3 "$DEST"/inject_nav.py

echo "== 4/5 启动桥接服务 =="
systemctl daemon-reload
systemctl enable asf-bridge
systemctl restart asf-bridge
sleep 1

echo "== 5/5 自检 =="
check_status_json() {
  python3 -c 'import json,sys; data=json.load(sys.stdin); print(json.dumps(data, ensure_ascii=False)); sys.exit(0 if data.get("asf_reachable") is True else 1)'
}

LOCAL_STATUS=$(curl --fail --silent --show-error --max-time 10 \
  -H "X-UI-Token: $TOKEN" --referer "https://$PUBLIC_DOMAIN/" \
  "http://127.0.0.1:$PORT/asf/api/status")
if ! printf '%s\n' "$LOCAL_STATUS" | check_status_json; then
  echo "ERROR: local bridge status JSON is invalid or ASF IPC is unreachable" >&2
  exit 1
fi

PUBLIC_STATUS=$(curl --fail --silent --show-error --max-time 15 \
  -H "X-UI-Token: $TOKEN" --referer "https://$PUBLIC_DOMAIN/" \
  "https://$PUBLIC_DOMAIN/asf/api/status")
if ! printf '%s\n' "$PUBLIC_STATUS" | check_status_json; then
  echo "ERROR: public bridge status JSON is invalid or ASF IPC is unreachable" >&2
  exit 1
fi

systemctl status asf-bridge --no-pager -n 5
rm -f "$ROLLBACK_BACKUP"
echo "部署完成。"
