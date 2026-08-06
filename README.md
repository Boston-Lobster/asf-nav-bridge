# ASF Nav Bridge

> 版本：**v1.0.0**

在个人导航页上为 ArchiSteamFarm 提供「一键重连并继续挂机」按钮与实时状态栏，
解决 ASF 网页端（ASF-ui）不显示手动 `play` 模式下挂机游戏的问题。

## 功能

- `GET /asf/api/status`：查询 ASF 连接状态、暂停状态、当前挂机游戏
- `POST /asf/api/reconnect`：自动执行 `resume` + `play`；账号未连接时先重启 ASF 再恢复
- 导航页卡片：重连按钮 + 每 15 秒自动刷新的状态栏
- 安全：仅监听本机、Nginx `/asf/` 反代、同源 Referer/Origin 校验 + `X-UI-Token` 头

## 快速开始

1. 复制 `config.example.json` 为 `config.json`，填入 ASF IPC 密码、bot 名、挂机游戏与 UI 令牌。
2. 同步修改 `nav-asf-snippet.html` 中的 `__UI_TOKEN__` 为同一个令牌。
3. 把本目录上传到服务器 `/opt/asf-bridge-staging/`，执行 `bash /opt/asf-bridge-staging/deploy.sh`。
   脚本会自动：安装 systemd 服务 → 注入 Nginx `/asf/` 路由（443 块）→ 注入导航页 → 自检。

Windows 环境也可直接运行 `deploy.ps1`（SSH 密钥路径可用环境变量 `ASF_SSH_KEY` 指定）。

## 状态栏说明

手动 `play` 模式下 ASF 的 `CardsFarmer.Paused` 恒为 `True`（挂卡模块暂停），且不会把
游戏列入 `CurrentGamesFarming`，因此状态栏在「已连接」时按配置显示目标挂机游戏，
并如实标注 play 手动模式说明。若 ASF 正在自动挂卡，则附加显示真实挂卡列表。

## 项目结构

```
asf-bridge.py          # 桥接服务（Python 标准库，无额外依赖）
asf-bridge.service     # systemd 单元
config.example.json    # 配置模板（复制为 config.json 后填写）
nav-asf-snippet.html   # 注入导航页的按钮 + 状态栏（含 UI 令牌占位符）
inject_nginx.py        # 幂等注入 Nginx /asf/ 路由（443 server 块）
inject_nav.py          # 幂等注入导航页控制块
deploy.sh / deploy.ps1 # 一键部署脚本
```

## 安全提醒

- `config.json` 含 ASF IPC 密码与 UI 令牌，已被 `.gitignore` 排除，请勿提交。
- UI 令牌同时写死在页面 JS 中，属于「提高门槛」而非强认证；如需更强保护，
  可给 `/asf/` 增加 Nginx 基本认证。
