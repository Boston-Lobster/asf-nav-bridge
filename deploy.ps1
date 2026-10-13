$ErrorActionPreference = 'Stop'
$bundle = $PSScriptRoot
$known = $env:ASF_SSH_KNOWN_HOSTS
if (-not $known) {
    $known = Join-Path $bundle 'known_hosts'
}
$stagingLocal = $bundle

# SSH 密钥路径：优先环境变量 ASF_SSH_KEY，否则使用默认占位路径
$key = $env:ASF_SSH_KEY
if (-not $key) {
    $key = 'YOUR_SSH_KEY_PATH'
}
if (-not (Test-Path -LiteralPath $key)) {
    throw ('找不到 SSH 密钥：' + $key + '（可设置环境变量 ASF_SSH_KEY）')
}

# 服务器地址：优先环境变量 ASF_SERVER_IP，否则使用占位值
$server = $env:ASF_SERVER_IP
if (-not $server -or $server -eq 'YOUR_SERVER_IP') {
    throw '请设置 ASF_SERVER_IP。'
}

if (-not (Test-Path -LiteralPath $known -PathType Leaf)) {
    throw ('找不到已核验的 SSH known_hosts 文件：' + $known +
        '。请先通过带外渠道核对主机指纹，再将主机公钥写入此文件；脚本不会自动信任首次连接的主机。')
}

$sshOptions = @(
    '-i', $key,
    '-o', 'BatchMode=yes',
    '-o', 'StrictHostKeyChecking=yes',
    '-o', "UserKnownHostsFile=$known",
    '-o', 'ConnectTimeout=15'
)

Write-Host '== 上传桥接服务文件 =='
& ssh @sshOptions "root@${server}" 'mkdir -p /opt/asf-bridge-staging'
if ($LASTEXITCODE -ne 0) {
    throw "SSH staging setup failed with exit code $LASTEXITCODE. Confirm the pinned host key is present in '$known' and verified out of band."
}

& scp @sshOptions `
    (Join-Path $stagingLocal 'asf-bridge.py') `
    (Join-Path $stagingLocal 'config.json') `
    (Join-Path $stagingLocal 'nav-asf-snippet.html') `
    (Join-Path $stagingLocal 'inject_nav.py') `
    (Join-Path $stagingLocal 'inject_nginx.py') `
    (Join-Path $stagingLocal 'asf-bridge.service') `
    (Join-Path $stagingLocal 'deploy.sh') `
    "root@${server}:/opt/asf-bridge-staging/"
if ($LASTEXITCODE -ne 0) {
    throw "SCP upload failed with exit code $LASTEXITCODE. Confirm the pinned host key is present in '$known' and verified out of band."
}

Write-Host '== 远程部署 =='
$remoteOverrides = @{}
if ($env:ASF_NAV_DOMAIN) {
    $remoteOverrides['ASF_NAV_DOMAIN'] = $env:ASF_NAV_DOMAIN
}
if ($env:ASF_NGINX_CONF) {
    $remoteOverrides['ASF_NGINX_CONF'] = $env:ASF_NGINX_CONF
}
$overrideJson = ConvertTo-Json -InputObject $remoteOverrides -Compress
$overrideBase64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($overrideJson))
$pythonCode = 'import base64,json,os,sys; env=os.environ.copy(); env.update(json.loads(base64.b64decode(sys.argv[1]))); os.execve("/bin/bash", ["bash", "/opt/asf-bridge-staging/deploy.sh"], env)'
$remoteDeploy = "python3 -c '$pythonCode' '$overrideBase64'"
& ssh @sshOptions "root@${server}" $remoteDeploy
if ($LASTEXITCODE -ne 0) {
    throw "Remote deployment failed with exit code $LASTEXITCODE. Confirm the pinned host key is present in '$known' and verified out of band."
}
