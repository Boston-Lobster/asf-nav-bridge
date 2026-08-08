#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ASF Bridge - 为导航页提供 ASF 状态查询与"重连并继续挂机"接口。
  GET  /asf/api/status     -> 当前状态 JSON
  POST /asf/api/reconnect  -> resume + play（必要时先重启 ASF），并返回最新状态

只监听 127.0.0.1，由 Nginx 从 /asf/ 前端反向代理；浏览器请求需带同源
Referer/Origin 且匹配 ui_token（X-UI-Token 头）。

v1.2.0 变更：优先读取 ASF 插件(AsfPlayStatus)暴露的 /api/playstatus 端点
（ASF 内存中的真实游玩列表），插件不可达时回退到本配置；重连成功后会
把目标挂机列表推送到插件内存并持久化。
"""

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CONFIG_PATH = os.environ.get("ASF_BRIDGE_CONFIG", "/opt/asf-bridge/config.json")

config = {}
_lock = threading.Lock()


def load_config():
    global config
    with open(CONFIG_PATH, "r", encoding="utf-8") as fp:
        config = json.load(fp)


def asf_request(method, path, payload=None, timeout=8):
    """向 ASF IPC 发起请求，返回 (status, json)。"""
    url = config["asf_ipc_url"].rstrip("/") + path
    headers = {
        "Authentication": config["asf_ipc_password"],
        "Accept": "application/json",
    }
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8", "replace")
        return resp.status, (json.loads(body) if body else None)


def asf_command(command):
    """执行 ASF 命令（如 resume/play），返回返回文本。"""
    _, data = asf_request("POST", "/api/command", {"Command": command})
    result = (data or {}).get("Result")
    return str(result) if result is not None else ""


def bot_info():
    """读取指定 bot 的实时信息；ASF 不可达时返回 None。"""
    try:
        _, data = asf_request("GET", "/api/bot/" + config["bot_name"])
        return data.get("Result", {}).get(config["bot_name"], {})
    except Exception:
        return None


def configured_play_apps():
    names = config.get("play_game_names", {})
    return [
        {"AppID": appid, "GameName": names.get(str(appid), str(appid))}
        for appid in config.get("play_appids", [])
    ]


def plugin_play_status():
    """读取 ASF 插件(AsfPlayStatus)内存中的正在游玩列表；失败返回 None。"""
    try:
        _, data = asf_request("GET", "/api/playstatus")
        result = (data or {}).get("Result", {}) or {}
        return result.get(config.get("bot_name", ""))
    except Exception:
        return None


def push_plugin_play_status():
    """把当前目标挂机列表写入 ASF 插件内存并持久化，供导航页实时读取。"""
    try:
        _, data = asf_request(
            "POST",
            "/api/playstatus",
            {
                "Bot": config["bot_name"],
                "AppIDs": config.get("play_appids", []),
                "GameNames": config.get("play_game_names", {}),
            },
        )
        return bool(data and data.get("Success"))
    except Exception:
        return False


def build_status(extra=None):
    info = bot_info()
    out = {
        "asf_reachable": info is not None,
        "connected": bool(info and info.get("IsConnectedAndLoggedOn")),
        "paused": bool(info and info.get("CardsFarmer", {}).get("Paused")),
        "farming": [],
        "playing": [],
        "play_source": "",
        "nickname": (info or {}).get("Nickname", ""),
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    if info:
        out["farming"] = info.get("CardsFarmer", {}).get("CurrentGamesFarming", []) or []
        out["nickname"] = info.get("Nickname", "")

    # 优先读取 ASF 插件(AsfPlayStatus)内存中的真实游玩列表；
    # 插件不可达或列表为空时，回退到桥接配置显示。
    plugin = plugin_play_status()
    if plugin and plugin.get("CurrentGames"):
        out["playing"] = plugin["CurrentGames"]
        out["play_source"] = plugin.get("Source", "")
    elif out["connected"]:
        out["playing"] = configured_play_apps()

    if extra:
        out.update(extra)
    return out


def do_reconnect():
    """恢复挂机：resume + play；若账号未连接则先让 ASF 整体重启再重试。"""
    with _lock:
        notes = []
        info = bot_info()
        if info is None:
            notes.append("ASF IPC 不可达，无法重连")
            return build_status({"notes": notes, "success": False})

        connected = bool(info.get("IsConnectedAndLoggedOn"))
        if not connected:
            notes.append("账号未连接，尝试重启 ASF")
            try:
                asf_command("restart")
                notes.append("已发送重启命令")
            except Exception as exc:
                notes.append("重启命令失败：%s" % exc)
            for _ in range(20):
                time.sleep(1)
                if bot_info() is not None:
                    break

        success = False
        try:
            r1 = asf_command("resume %s" % config["bot_name"])
            r2 = asf_command(
                "play %s %s"
                % (config["bot_name"], ",".join(str(a) for a in config.get("play_appids", [])))
            )
            notes.append("resume -> %s" % (r1 or "已执行"))
            notes.append("play -> %s" % (r2 or "已执行"))
            if push_plugin_play_status():
                notes.append("插件游玩列表已同步")
            else:
                notes.append("插件游玩列表同步失败(端点不可达)")
            success = True
        except Exception as exc:
            notes.append("命令执行失败：%s" % exc)

        time.sleep(1)
        return build_status({"notes": notes, "success": success})


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        # 静默常规访问日志，避免刷屏
        pass

    def _reply(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self):
        token = config.get("ui_token")
        if token and self.headers.get("X-UI-Token", "") != token:
            return False
        host = ""
        for header in ("Origin", "Referer"):
            value = self.headers.get(header) or ""
            if value:
                try:
                    host = urllib.parse.urlparse(value).netloc.lower().split(":")[0]
                except Exception:
                    host = ""
                if host:
                    break
        if not host:
            return False  # 浏览器同源请求必然带 Origin/Referer
        allowed = {item.lower() for item in config.get("allowed_origins", [])}
        return host in allowed

    def do_GET(self):
        if self.path.split("?")[0] == "/asf/api/status":
            if not self._authorized():
                self._reply(403, {"success": False, "error": "forbidden"})
                return
            self._reply(200, build_status())
        else:
            self._reply(404, {"success": False, "error": "not found"})

    def do_POST(self):
        if self.path.split("?")[0] == "/asf/api/reconnect":
            if not self._authorized():
                self._reply(403, {"success": False, "error": "forbidden"})
                return
            self._reply(200, do_reconnect())
        else:
            self._reply(404, {"success": False, "error": "not found"})


def main():
    load_config()
    server = ThreadingHTTPServer((config["listen_host"], config["listen_port"]), Handler)
    print(
        "ASF bridge listening on %s:%s"
        % (config["listen_host"], config["listen_port"]),
        flush=True,
    )
    server.serve_forever()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("ASF bridge failed to start: %s" % exc, file=sys.stderr)
        sys.exit(1)
