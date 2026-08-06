$ErrorActionPreference = 'Stop'
$bundle = $PSScriptRoot
$known = Join-Path $bundle 'known_hosts'
$stagingLocal = $bundle

# SSH 密钥路径：优先环境变量 ASF_SSH_KEY，否则使用默认桌面路径
$key = $env:ASF_SSH_KEY
if (-not $key) {
    $key = 'YOUR_SSH_KEY_PATH'
}
if (-not (Test-Path -LiteralPath $key)) {
    throw '找不到 SSH 密钥：' + $key + '（可设置环境变量 ASF_SSH_KEY）'
}

# 服务器地址：优先环境变量 ASF_SERVER_IP，否则请手动替换占位符
$server = $env:ASF_SERVER_IP
if (-not $server) {
    $server = 'YOUR_SERVER_IP'
}

Write-Host '== 上传桥接服务文件 =='
ssh -i $key -o BatchMode=yes -o ConnectTimeout=15 -o UserKnownHostsFile=$known "root@${server}" "mkdir -p /opt/asf-bridge-staging"
scp -i $key -o BatchMode=yes -o UserKnownHostsFile=$known (Join-Path $stagingLocal 'asf-bridge.py') (Join-Path $stagingLocal 'config.json') (Join-Path $stagingLocal 'nav-asf-snippet.html') (Join-Path $stagingLocal 'inject_nav.py') (Join-Path $stagingLocal 'inject_nginx.py') (Join-Path $stagingLocal 'asf-bridge.service') (Join-Path $stagingLocal 'deploy.sh') "root@${server}:/opt/asf-bridge-staging/"

Write-Host '== 远程部署 =='
ssh -i $key -o BatchMode=yes -o ConnectTimeout=15 -o UserKnownHostsFile=$known "root@${server}" "bash /opt/asf-bridge-staging/deploy.sh"
