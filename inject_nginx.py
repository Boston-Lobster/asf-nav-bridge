#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""向 HTTPS server 块注入 /asf/ 反代 location（幂等，自动移除错位块）。"""

import re
import os
import shutil
import sys

CONF = sys.argv[1] if len(sys.argv) > 1 else "/etc/nginx/sites-available/YOUR_SITE"
MARKER = "asf-bridge-location"

location_block = """    # asf-bridge-location
    location /asf/ {
        proxy_pass http://127.0.0.1:17001;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
"""

with open(CONF, "r", encoding="utf-8") as fp:
    content = fp.read()

# 1) 移除任何已存在的 asf-bridge-location 块（含误插到 location / 内的）
pattern = re.compile(
    r"[ \t]*# asf-bridge-location[ \t]*\n"
    r"[ \t]*location /asf/ \{[ \t]*\n"
    r"(?:[^\n]*\n)*?"
    r"[ \t]*\}\n?",
    re.MULTILINE,
)
new_content, removed = pattern.subn("", content)

# 2) 找到 443 server 块内的 server_name 行，在其后插入
idx = new_content.find("listen 443")
if idx == -1:
    print("ERROR: cannot find 'listen 443' in %s" % CONF, file=sys.stderr)
    sys.exit(1)
server_name_idx = new_content.rfind("server_name", 0, idx)
if server_name_idx == -1:
    print("ERROR: cannot find server_name inside 443 block", file=sys.stderr)
    sys.exit(1)
line_end = new_content.find("\n", server_name_idx)
if line_end == -1:
    line_end = len(new_content)
insert_at = line_end + 1

backup = CONF + ".bak-asf-bridge"
if not os.path.exists(backup):
    shutil.copy2(CONF, backup)

final = new_content[:insert_at] + "\n" + location_block + new_content[insert_at:]
with open(CONF, "w", encoding="utf-8") as fp:
    fp.write(final)
print(
    "nginx injected into 443 server block%s, backup at %s"
    % (" (removed %d misplaced block(s))" % removed if removed else "", backup)
)
