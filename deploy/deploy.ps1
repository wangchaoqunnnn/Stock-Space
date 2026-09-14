<#
=============================================================================
 StockSpace 一键部署脚本（Windows / PowerShell）
=============================================================================
 支持两种方式：
   1) Docker 部署（推荐，需要 Docker Desktop 已启动）
   2) 直接部署（本机 Python + 后台进程；可选注册计划任务实现开机自启）

 用法：
   pwsh -File deploy\deploy.ps1                       # 自动选择
   pwsh -File deploy\deploy.ps1 -Mode docker
   pwsh -File deploy\deploy.ps1 -Mode direct -Port 8770
   pwsh -File deploy\deploy.ps1 -RegisterTask         # 直接部署并注册开机自启

 幂等性：venv 已存在则复用；.env 只补缺失键；重复执行安全。
=============================================================================
#>
[CmdletBinding()]
param(
    [ValidateSet('auto', 'docker', 'direct')]
    [string]$Mode = 'auto',
    [int]$Port = 0,
    [switch]$RegisterTask,
    [switch]$SkipBuild,
    [switch]$Help
)

$ErrorActionPreference = 'Stop'

function Write-Title($text) { Write-Host ""; Write-Host $text -ForegroundColor Cyan }
function Write-Log($text)   { Write-Host "[$(Get-Date -Format 'HH:mm:ss')] $text" }
function Write-Ok($text)    { Write-Host "[ OK ] $text" -ForegroundColor Green }
function Write-Warn2($text) { Write-Host "[WARN] $text" -ForegroundColor Yellow }
function Write-Err2($text)  { Write-Host "[FAIL] $text" -ForegroundColor Red }
function Die($text)         { Write-Err2 $text; exit 1 }

if ($Help) {
    Get-Help $PSCommandPath -Detailed
    exit 0
}

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$AppDir = Split-Path -Parent $ScriptDir
Set-Location $AppDir

Write-Title "StockSpace 部署（Windows）"
Write-Log "项目目录: $AppDir"

# ------------------------------------------------------------------ 生成 .env
Write-Title "1/6 配置文件"
$EnvFile = Join-Path $AppDir '.env'
$EnvExample = Join-Path $AppDir '.env.example'
if (-not (Test-Path $EnvFile)) {
    if (Test-Path $EnvExample) {
        Copy-Item $EnvExample $EnvFile
        Write-Ok "已从 .env.example 生成 .env"
    } else {
        New-Item -ItemType File -Path $EnvFile | Out-Null
        Write-Ok "已创建空的 .env"
    }
} else {
    Write-Log ".env 已存在，仅补充缺失键"
}

function Ensure-Env([string]$key, [string]$value) {
    $content = if (Test-Path $EnvFile) { Get-Content $EnvFile -Raw } else { '' }
    if ($content -notmatch "(?m)^$([regex]::Escape($key))=") {
        Add-Content -Path $EnvFile -Value "$key=$value" -Encoding utf8
        Write-Log "  补充 $key=$value"
    }
}

# 读取 .env 里的端口（未指定 -Port 时）
if ($Port -le 0) {
    $content = Get-Content $EnvFile -Raw -ErrorAction SilentlyContinue
    $m = [regex]::Match($content, '(?m)^HOST_PORT=(\d+)')
    if (-not $m.Success) { $m = [regex]::Match($content, '(?m)^SS_SERVER__PORT=(\d+)') }
    $Port = if ($m.Success) { [int]$m.Groups[1].Value } else { 8770 }
}
Ensure-Env 'HOST_PORT' $Port
Ensure-Env 'SS_SERVER__PORT' $Port
Ensure-Env 'SS_SERVER__HOST' '0.0.0.0'
Ensure-Env 'SS_DATA_SOURCES__MODE' 'auto'
Ensure-Env 'SS_QUOTAS__UNIVERSE_SIZE' '0'
Ensure-Env 'SS_PUSH__ENABLED' 'false'
Write-Ok "端口: $Port"

# ------------------------------------------------------------------ 数据目录
New-Item -ItemType Directory -Force -Path (Join-Path $AppDir 'data'), (Join-Path $AppDir 'logs'), (Join-Path $AppDir 'config') | Out-Null

# ------------------------------------------------------------------ 选择模式
$dockerOk = $false
if (Get-Command docker -ErrorAction SilentlyContinue) {
    try { docker info *> $null; $dockerOk = $true } catch { $dockerOk = $false }
}
if ($Mode -eq 'auto') {
    $Mode = if ($dockerOk) { 'docker' } else { 'direct' }
    Write-Log "自动选择部署方式: $Mode"
}
if ($Mode -eq 'docker' -and -not $dockerOk) {
    Die "Docker 不可用（未安装或 Docker Desktop 未启动）。可改用 -Mode direct"
}

# ============================================================ Docker 部署
if ($Mode -eq 'docker') {
    Write-Title "2/6 构建镜像"
    if (-not $SkipBuild) {
        Write-Log "开始构建（首次约 3~8 分钟）…"
        docker compose build
        if ($LASTEXITCODE -ne 0) { Die "镜像构建失败" }
        Write-Ok "镜像构建完成"
    }

    Write-Title "3/6 启动容器"
    docker compose up -d --remove-orphans
    if ($LASTEXITCODE -ne 0) { Die "容器启动失败" }
    Write-Ok "容器已启动"

    Write-Title "4/6 等待健康检查"
    $healthy = $false
    for ($i = 1; $i -le 60; $i++) {
        try {
            $r = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/healthz" -TimeoutSec 4
            if ($r.status -eq 'ok') { $healthy = $true; break }
        } catch { }
        Start-Sleep -Seconds 5
        if ($i % 6 -eq 0) { Write-Log "  等待中… ($i/60)" }
    }
    if ($healthy) { Write-Ok "服务健康检查通过" }
    else {
        Write-Warn2 "健康检查未通过，最近日志："
        docker compose logs --tail 40
    }

    Write-Title "5/6 放行防火墙端口（本机）"
    try {
        $rule = Get-NetFirewallRule -DisplayName "StockSpace $Port" -ErrorAction SilentlyContinue
        if (-not $rule) {
            New-NetFirewallRule -DisplayName "StockSpace $Port" -Direction Inbound -Action Allow `
                -Protocol TCP -LocalPort $Port -ErrorAction Stop | Out-Null
            Write-Ok "已添加防火墙入站规则 TCP $Port"
        } else {
            Write-Log "防火墙规则已存在"
        }
    } catch {
        Write-Warn2 "添加防火墙规则失败（可能需要管理员权限）：$($_.Exception.Message)"
    }

    Write-Title "6/6 完成"
    Write-Host ""
    Write-Host "  访问地址 : http://127.0.0.1:$Port/" -ForegroundColor White
    Write-Host "  部署自检 : http://127.0.0.1:$Port/selfcheck"
    Write-Host "  接口文档 : http://127.0.0.1:$Port/docs"
    Write-Host ""
    Write-Host "  查看日志 : docker compose logs -f --tail=200"
    Write-Host "  重启服务 : docker compose restart"
    Write-Host "  停止服务 : docker compose down"
    Write-Host "  升级部署 : git pull; docker compose up -d --build"
    Write-Host ""
    exit 0
}

# ============================================================ 直接部署
Write-Title "2/6 检查 Python"
$python = $null
foreach ($candidate in @('py', 'python', 'python3')) {
    $cmd = Get-Command $candidate -ErrorAction SilentlyContinue
    if ($cmd) {
        # 跳过 Microsoft Store 的占位程序（它会直接失败并提示去商店）
        try {
            $ver = & $candidate -c "import sys;print('%d.%d' % sys.version_info[:2])" 2>$null
            if ($LASTEXITCODE -eq 0 -and $ver) { $python = $candidate; $pyver = $ver; break }
        } catch { }
    }
}
if (-not $python) {
    Die "未找到可用的 Python。请安装 Python 3.10+ 并勾选 'Add python.exe to PATH'，或改用 -Mode docker"
}
Write-Ok "使用 $python（Python $pyver）"

$venvDir = Join-Path $AppDir '.venv'
$venvPy = Join-Path $venvDir 'Scripts\python.exe'
if (Test-Path $venvPy) {
    Write-Log "复用已存在的虚拟环境"
} else {
    Write-Log "创建虚拟环境…"
    if ($python -eq 'py') { & py -3 -m venv $venvDir } else { & $python -m venv $venvDir }
    if ($LASTEXITCODE -ne 0) { Die "虚拟环境创建失败" }
    Write-Ok "虚拟环境已创建"
}

Write-Title "3/6 安装依赖"
& $venvPy -m pip install --quiet --upgrade pip setuptools wheel
if ($LASTEXITCODE -ne 0) { Write-Warn2 "pip 升级失败，继续尝试安装依赖" }
& $venvPy -m pip install --quiet -r (Join-Path $AppDir 'backend\requirements.txt')
if ($LASTEXITCODE -ne 0) {
    Write-Warn2 "默认源安装失败，尝试国内镜像…"
    & $venvPy -m pip install --quiet `
        -i https://pypi.tuna.tsinghua.edu.cn/simple `
        --extra-index-url https://pypi.mirrors.ustc.edu.cn/simple `
        -r (Join-Path $AppDir 'backend\requirements.txt')
    if ($LASTEXITCODE -ne 0) { Die "依赖安装失败" }
}
Write-Ok "运行依赖已安装"

Write-Title "4/6 环境自检"
& $venvPy (Join-Path $AppDir 'backend\run.py') --check

Write-Title "5/6 启动服务"
$logFile = Join-Path $AppDir 'logs\console.log'
$errFile = Join-Path $AppDir 'logs\console.err.log'
$existing = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($existing) {
    Write-Warn2 "端口 $Port 已被占用，尝试停止旧进程…"
    $existing | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Seconds 2
}

$proc = Start-Process -FilePath $venvPy `
    -ArgumentList @((Join-Path $AppDir 'backend\run.py'), '--port', $Port) `
    -WorkingDirectory (Join-Path $AppDir 'backend') `
    -RedirectStandardOutput $logFile -RedirectStandardError $errFile `
    -WindowStyle Hidden -PassThru
Write-Ok "服务已启动（PID $($proc.Id)）"

$healthy = $false
for ($i = 1; $i -le 60; $i++) {
    try {
        $r = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/healthz" -TimeoutSec 4
        if ($r.status -eq 'ok') { $healthy = $true; break }
    } catch { }
    Start-Sleep -Seconds 5
    if ($i % 6 -eq 0) { Write-Log "  等待中… ($i/60)" }
}
if ($healthy) { Write-Ok "服务健康检查通过" }
else {
    Write-Warn2 "健康检查未通过，日志尾部："
    if (Test-Path $errFile) { Get-Content $errFile -Tail 30 | ForEach-Object { Write-Host "    $_" } }
}

if ($RegisterTask) {
    Write-Log "注册开机自启计划任务…"
    try {
        $action = New-ScheduledTaskAction -Execute $venvPy `
            -Argument (Join-Path $AppDir 'backend\run.py') `
            -WorkingDirectory (Join-Path $AppDir 'backend')
        $trigger = New-ScheduledTaskTrigger -AtStartup
        $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
            -LogonType S4U -RunLevel Highest
        $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
            -DontStopIfGoingOnBatteries -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
        Register-ScheduledTask -TaskName 'StockSpace' -Action $action -Trigger $trigger `
            -Principal $principal -Settings $settings -Force | Out-Null
        Write-Ok "已注册计划任务 StockSpace（开机自启）"
    } catch {
        Write-Warn2 "注册计划任务失败：$($_.Exception.Message)"
    }
}

try {
    $rule = Get-NetFirewallRule -DisplayName "StockSpace $Port" -ErrorAction SilentlyContinue
    if (-not $rule) {
        New-NetFirewallRule -DisplayName "StockSpace $Port" -Direction Inbound -Action Allow `
            -Protocol TCP -LocalPort $Port -ErrorAction Stop | Out-Null
        Write-Ok "已添加防火墙入站规则 TCP $Port"
    }
} catch {
    Write-Warn2 "添加防火墙规则失败（可能需要管理员权限）"
}

Write-Title "6/6 完成"
Write-Host ""
Write-Host "  访问地址 : http://127.0.0.1:$Port/" -ForegroundColor White
Write-Host "  部署自检 : http://127.0.0.1:$Port/selfcheck"
Write-Host "  接口文档 : http://127.0.0.1:$Port/docs"
Write-Host ""
Write-Host "  服务 PID : $($proc.Id)"
Write-Host "  运行日志 : $logFile"
Write-Host "  错误日志 : $errFile"
Write-Host "  停止服务 : Stop-Process -Id $($proc.Id)"
Write-Host ""
