#!/bin/bash
# ASF Bridge + 导航页 部署脚本（在服务器上以 root 运行）
set -e

STAGING=/opt/asf-bridge-staging
DEST=/opt/asf-bridge
# 部署前替换为你的域名（或设置环境变量 ASF_NAV_DOMAIN）
DOMAIN="${ASF_NAV_DOMAIN:-YOUR_DOMAIN}"

echo "== 1/5 安装文件 =="
mkdir -p "$DEST"
cp -f "$STAGING"/asf-bridge.py "$STAGING"/config.json "$STAGING"/nav-asf-snippet.html \
      "$STAGING"/inject_nav.py "$STAGING"/inject_nginx.py "$DEST"/
cp -f "$STAGING"/asf-bridge.service /etc/systemd/system/asf-bridge.service
chmod 600 "$DEST"/config.json
chmod 755 "$DEST"/asf-bridge.py

echo "== 2/5 注入 Nginx 配置 =="
python3 "$DEST"/inject_nginx.py
nginx -t
systemctl reload nginx

echo "== 3/5 注入导航页 =="
python3 "$DEST"/inject_nav.py

echo "== 4/5 启动桥接服务 =="
systemctl daemon-reload
systemctl enable --now asf-bridge
sleep 1

echo "== 5/5 自检 =="
TOKEN=$(python3 -c "import json;print(json.load(open('$DEST/config.json'))['ui_token'])" 2>/dev/null || true)
curl -s -H "X-UI-Token: $TOKEN" --referer "https://$DOMAIN/" \
  http://127.0.0.1:17001/asf/api/status
echo
curl -s -o /dev/null -w "public /asf/api/status -> %{http_code}\n" \
  --referer "https://$DOMAIN/" "https://$DOMAIN/asf/api/status"
systemctl status asf-bridge --no-pager -n 5
echo "部署完成。"
