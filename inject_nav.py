#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 ASF 控制块注入导航页 /opt/nav/index.html（幂等，先备份）。"""

import os
import shutil
import sys

NAV = sys.argv[1] if len(sys.argv) > 1 else "/opt/nav/index.html"
SNIPPET = sys.argv[2] if len(sys.argv) > 2 else "/opt/asf-bridge/nav-asf-snippet.html"
MARKER = "asf-control-start"

if not os.path.exists(NAV):
    print("ERROR: %s not found" % NAV, file=sys.stderr)
    sys.exit(1)

with open(NAV, "r", encoding="utf-8") as fp:
    html = fp.read()

with open(SNIPPET, "r", encoding="utf-8") as fp:
    snippet = fp.read()

backup = NAV + ".bak-asf-bridge"
if not os.path.exists(backup):
    shutil.copy2(NAV, backup)

start_marker = "<!-- asf-control-start -->"
end_marker = "<!-- asf-control-end -->"
start = html.find(start_marker)
if start != -1:
    end = html.find(end_marker)
    if end == -1:
        print("ERROR: found start marker but no end marker", file=sys.stderr)
        sys.exit(1)
    end += len(end_marker)
    html = html[:start] + snippet + html[end:]
    print("nav page block replaced")
elif "</body>" in html:
    html = html.replace("</body>", snippet + "\n</body>", 1)
    print("nav page injected, backup at %s" % backup)
else:
    html = html + "\n" + snippet
    print("nav page injected (appended), backup at %s" % backup)

with open(NAV, "w", encoding="utf-8") as fp:
    fp.write(html)
