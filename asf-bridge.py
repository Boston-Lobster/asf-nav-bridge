#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ASF Bridge - 为导航页提供 ASF 状态查询与"重连并继续挂机"接口。
  GET  /asf/api/status     -> 当前状态 JSON
  POST /asf/api/reconnect  -> 等待登录，结构化执行 play，并返回最新状态

只监听 127.0.0.1，由 Nginx 从 /asf/ 前端反向代理；浏览器请求需带同源
Referer/Origin 且匹配 ui_token（X-UI-Token 头）。

v1.2.0 变更：优先读取 ASF 插件(AsfPlayStatus)暴露的 /api/playstatus 端点
（ASF 内存中的真实游玩列表），插件不可达时回退到本配置；重连成功后会
把目标挂机列表推送到插件内存并持久化。

v1.3.0 变更：版本对齐（无功能变更）。

v1.3.1 变更：隐私处理——适配插件输出中的账号名掩码（bot1/bot2...），
状态接口不再返回账号昵称。

v1.4.1：依赖 AsfPlayStatus >=1.3.1 的 Play 端点，按目标 bot 查询；
区分目标列表与当前状态，重连失败不再写入虚假游玩列表。
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
VERSION = "1.4.1"

config = {}
_lock = threading.Lock()


def load_config():
    global config
    with open(CONFIG_PATH, "r", encoding="utf-8") as fp:
        config = json.load(fp)
    if not config.get("ui_token") or config["ui_token"] == "CHANGE_ME":
        raise ValueError("请配置非空且非占位符的 ui_token")
    if not config.get("bot_name") or config["bot_name"] == "YOUR_BOT":
        raise ValueError("请配置真实 bot_name")
    timeout = config.get("reconnect_timeout", 60)
    if not isinstance(timeout, (int, float)) or not 1 <= timeout <= 300:
        raise ValueError("reconnect_timeout 必须为 1 到 300 秒")
    appids = config.get("play_appids", [])
    if (not isinstance(appids, list) or not appids
            or any(type(appid) is not int or not 0 < appid <= 4294967295 for appid in appids)
            or len(appids) > 32 or len(set(appids)) != len(appids)):
        raise ValueError("play_appids 必须含 1 到 32 个非零 uint AppID")


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


def asf_action(path, payload=None):
    """执行结构化动作；HTTP 200 不代表业务成功。"""
    _, data = asf_request("POST", path, payload)
    if not isinstance(data, dict) or data.get("Success") is not True:
        message = data.get("Message", "ASF 动作失败") if isinstance(data, dict) else "ASF 响应无效"
        raise RuntimeError(message)
    return data


def bot_info():
    """读取指定 bot 的实时信息；ASF 不可达时返回 None。"""
    try:
        _, data = asf_request("GET", "/api/bot/" + urllib.parse.quote(config["bot_name"], safe=""))
        result = data.get("Result", {}) if isinstance(data, dict) else {}
        info = result.get(config["bot_name"]) if isinstance(result, dict) else None
        return info if isinstance(info, dict) else None
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
        query = urllib.parse.urlencode({"Bot": config["bot_name"]})
        _, data = asf_request("GET", "/api/playstatus?" + query)
        if not isinstance(data, dict) or data.get("Success") is not True:
            return None
        result = (data or {}).get("Result", {}) or {}
        if not isinstance(result, dict) or len(result) != 1:
            return None
        state = next(iter(result.values()))
        if not isinstance(state, dict) or not isinstance(state.get("CurrentGames"), list):
            return None
        return state
    except Exception:
        return None


def play_configured_games():
    """由插件执行真实 ASF play 动作，并在成功后记录目标列表。"""
    return asf_action("/api/playstatus/play", {
        "Bot": config["bot_name"],
        "AppIDs": config.get("play_appids", []),
        "GameNames": config.get("play_game_names", {}),
    })


def build_status(extra=None):
    info = bot_info()
    out = {
        "asf_reachable": info is not None,
        "connected": bool(info and info.get("IsConnectedAndLoggedOn")),
        "paused": bool(info and info.get("CardsFarmer", {}).get("Paused")),
        "farming": [],
        "playing": [],
        "play_source": "",
        "play_known": False,
        "target_games": configured_play_apps(),
        "version": VERSION,
        "nickname": "",  # 隐私处理：不再返回账号昵称
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    if info:
        out["farming"] = info.get("CardsFarmer", {}).get("CurrentGamesFarming", []) or []

    # 空列表也是有效状态。配置只用于 target_games，不假装是实时 playing。
    plugin = plugin_play_status()
    if plugin is not None:
        out["playing"] = plugin["CurrentGames"] if out["connected"] else []
        out["play_source"] = plugin.get("Source", "")
        out["play_known"] = True

    if extra:
        out.update(extra)
    return out


def do_reconnect():
    """恢复手动挂机：确认登录后，由插件执行并确认 play 动作。"""
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
                asf_action("/api/asf/restart")
                notes.append("已发送重启命令")
            except Exception as exc:
                notes.append("重启命令失败：%s" % exc)
            deadline = time.monotonic() + config.get("reconnect_timeout", 60)
            while time.monotonic() < deadline:
                time.sleep(1)
                info = bot_info()
                if info and info.get("IsConnectedAndLoggedOn"):
                    break
            else:
                notes.append("等待 Steam 登录超时，未发送 play")
                return build_status({"notes": notes, "success": False})

        success = False
        try:
            play_configured_games()
            notes.append("ASF 已接受 play，插件列表已持久化")
            success = True
        except urllib.error.HTTPError as exc:
            notes.append("游玩端点失败（HTTP %s）；请确认 AsfPlayStatus >=1.3.1" % exc.code)
        except Exception as exc:
            notes.append("命令执行失败：%s" % exc)

        time.sleep(1)
        status = build_status({"notes": notes, "success": success})
        if success and not status["connected"]:
            status["success"] = False
            notes.append("play 后连接已断开，请稍后重试")
        return status


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
        if not token or self.headers.get("X-UI-Token", "") != token:
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
    if sys.argv[1:] == ["--check-config"]:
        print("ASF bridge configuration valid", flush=True)
        return
    if sys.argv[1:]:
        raise ValueError("usage: asf-bridge.py [--check-config]")
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

