# ============================================================
# upsbridge Windows 开机自启安装（计划任务，无需登录即启动）
# 以管理员身份运行：
#   powershell -ExecutionPolicy Bypass -File install-autostart.ps1
# 配置固定在 %ProgramData%\upsbridge\config.json（SYSTEM 运行，开机即服务）
# ============================================================
$ErrorActionPreference = "Stop"

# 仓库根：windows/ 的上一级
$repo = Split-Path -Parent $PSScriptRoot
$script = Join-Path $repo "app\target\upsbridge\upsbridge.py"
if (-not (Test-Path $script)) { Write-Host "找不到主程序 $script"; exit 1 }

# 找 Python（pythonw 无控制台窗口；退而求其次 pyw / python）
$py = $null
foreach ($c in @("pythonw.exe", "pyw.exe", "python.exe")) {
  $cmd = Get-Command $c -ErrorAction SilentlyContinue
  if ($cmd) { $py = $cmd.Source; break }
}
if (-not $py) {
  Write-Host "找不到 Python，请先安装 Python 3.11+（勾选 Add to PATH）"
  exit 1
}

$confDir = Join-Path $env:ProgramData "upsbridge"
New-Item -ItemType Directory -Force -Path $confDir | Out-Null
$conf = Join-Path $confDir "config.json"
$log  = Join-Path $confDir "upsbridge.log"

# 单引号串里用双引号拼接，避免转义问题
$tr = [char]34 + $py + [char]34 + " " + [char]34 + $script + [char]34 + " --config " + [char]34 + $conf + [char]34 + " --log " + [char]34 + $log + [char]34 + " --port 28190"

schtasks /Create /F /TN "upsbridge" /SC ONSTART /RU SYSTEM /TR $tr | Out-Null
if ($LASTEXITCODE -ne 0) { Write-Host "创建计划任务失败（需要管理员权限）"; exit 1 }
schtasks /Run /TN "upsbridge" | Out-Null

# 防火墙放行管理界面端口
netsh advfirewall firewall delete rule name="upsbridge" >$null 2>$null
netsh advfirewall firewall add rule name="upsbridge" dir=in action=allow protocol=TCP localport=28190 | Out-Null

Write-Host "安装完成："
Write-Host "  计划任务 upsbridge（开机自启，SYSTEM 运行）"
Write-Host ("  配置文件 " + $conf)
Write-Host ("  运行日志 " + $log)
Write-Host "  管理界面 http://127.0.0.1:28190"
Write-Host ""
Write-Host "注意：开机自启用独立配置（ProgramData），"
Write-Host "      与双击 run.bat 的 APPDATA 配置互不相通，二选一使用。"
