# ASF Nav Bridge

> 版本：**v1.4.1** ｜ 配套插件：**AsfPlayStatus >= v1.3.1**

为个人导航页提供 ASF 连接状态、挂卡列表和“重新连接并继续挂机”按钮。Python 标准库实现，经 Nginx 同源反向代理提供接口。

## 本版修复

- 按目标 bot 查询插件状态，避免多 bot 状态串用。
- 等待 Steam 登录后，通过插件的结构化 Play 端点执行动作；失败不会被标成成功。
- 插件返回空列表时保持为空；插件不可达时显示“未知”，配置只作为目标列表。
- 正确定位 HTTPS server，部署验证失败会恢复本次 Nginx 配置。
- 重新部署重启桥接进程；PowerShell 原生命令失败立即停止。
- 前端保留重连结果，手动列表标为最近同步目标，避免声称 Steam 正在计时。

完整记录见 [CHANGELOG.md](CHANGELOG.md)。

## 接口

- `GET /asf/api/status`：连接、挂卡及插件状态。新增 `play_known` 和 `target_games`；`playing` 为空与状态未知分别表达。
- `POST /asf/api/reconnect`：账号未连接时尝试请求 ASF 重启并等待登录；已连接后请求 `POST /Api/PlayStatus/Play`，该端点只在 ASF 接受动作后记录列表。
- 两个接口都需要匹配的 `X-UI-Token`，以及允许的 Origin/Referer 主机。

`reconnect_timeout` 默认 60 秒，可配置为 1–300 秒。ASF 使用 `--no-restart` 时会拒绝 IPC 重启；桥接仍等待 ASF 自身重新连接，超时返回失败，不会强制操作 systemd。

旧版 AsfPlayStatus 不支持 Play 端点；请先升级插件再升级桥接。读状态时若旧插件返回多个 bot，桥接会拒绝猜测目标。

## 安装与升级

1. 安装 [AsfPlayStatus v1.3.1 或以上](https://github.com/Boston-Lobster/asf-playstatus/releases)，重启 ASF。
2. 复制 `config.example.json` 为 `config.json`，填写 IPC 地址/密码、真实 bot 名、非零游戏 AppID、域名和非占位符 UI token。
3. 修改 `nav-asf-snippet.html` 中的 `var TOKEN = 'CHANGE_ME'`，与配置 token 保持一致。
4. 将文件放到服务器 `/opt/asf-bridge-staging/`；确认导航页路径和 Nginx 配置路径适合当前站点，再执行 `bash /opt/asf-bridge-staging/deploy.sh`。
5. 以浏览器访问导航页核对状态和重连结果。

配置中 `play_appids` 必须有 1–32 个不同的非零 uint AppID。游戏名仅用于显示。停止手动游玩或执行 reset/resume 后，同步客户端应向 AsfPlayStatus POST 空列表以清空历史目标；插件不能从 Steam 查询真实手动计时列表。

`ASF_NAV_DOMAIN` 可指定目标域名，`ASF_NGINX_CONF` 可指定站点配置文件。多个 HTTPS server 时必须提供能唯一匹配的目标域名。改变监听端口时同时检查部署生成的反代目标。

Windows 可使用 `deploy.ps1`，通过环境变量 `ASF_SERVER_IP` 和 `ASF_SSH_KEY` 指定服务器和密钥。首次连接前，应将已核对指纹的 SSH 主机公钥写入本目录 `known_hosts`；部署使用严格主机密钥检查，不会自动信任未知主机。

## 状态的含义

- Source=Farming：来自 ASF 的自动挂卡列表。
- Source=Manual：最近成功同步的手动目标；停止/改变目标后需要同步清空或替换。ASF 接受 play 不等于 Steam 已确认时长增加。
- `play_known=false`：插件状态不可用，`playing=[]`，另提供 `target_games` 供客户端展示配置目标。
- 断线时 `playing=[]`，不会显示历史列表为正在游玩。

UI token 放在浏览器页面中，因此不能代替账户认证。需要限制站点访问时，可使用已有的 Nginx 认证策略。IPC 密码只保存在服务器 `config.json` 中，配置文件不进入源码包或 GitHub。

## 验证和发布

`python -m unittest discover -s tests -v` 运行离线回归测试。服务器上另运行 `bash -n deploy.sh` 和 Nginx 配置检查。

默认分支的 GitHub Actions 在测试通过后，为新版本创建 `v版本号` 标签和 GitHub Release，附源码包与 SHA-256 校验文件。已有标签/Release 保留，发布流程不会移动历史标签。版本同时维护于代码、VERSION、README 和 CHANGELOG。

## 文件

`asf-bridge.py`、systemd 单元、配置模板、导航页片段、Nginx/导航页注入脚本、Windows/Linux 部署脚本、tests、打包脚本和发布工作流。

