# upsbridge Windows 版

把局域网里任意 NUT Server 的断电事件，变成这台 Windows 机器的关机动作。核心程序与 fnOS 版是同一份 `app/target/upsbridge/upsbridge.py`，只多了平台感知（默认关机命令 `shutdown /s /t 0`、配置与日志路径）。

## 前置条件

- Windows 10 / 11 或 Windows Server 2016+
- Python 3.11+（安装时勾选 **Add python.exe to PATH**）
- 与 NUT Server 同一局域网，3493/tcp 可达

## 方式一：临时运行（试用 / 调试）

双击 `run.bat`，浏览器打开 <http://127.0.0.1:28190>。

- 配置：`%APPDATA%\upsbridge\config.json`
- 日志：`%APPDATA%\upsbridge\upsbridge.log`
- 关窗口即退出，不常驻。

## 方式二：开机自启（推荐生产使用）

右键 `install-autostart.ps1` → **使用 PowerShell 运行**（需管理员），或：

```powershell
powershell -ExecutionPolicy Bypass -File install-autostart.ps1
```

它会：

1. 创建计划任务 `upsbridge`（**开机即启动，无需登录**，以 SYSTEM 运行）；
2. 放行防火墙入站 28190 端口；
3. 立即启动一次。

- 配置：`C:\ProgramData\upsbridge\config.json`
- 日志：`C:\ProgramData\upsbridge\upsbridge.log`

卸载自启：管理员运行 `uninstall-autostart.ps1`（只删计划任务和防火墙规则，配置日志保留）。

> **两种方式的配置互不相通**：`run.bat` 用当前用户的 APPDATA，自启用 ProgramData（SYSTEM 身份）。选一种用，别混。

## 首次配置

1. 打开 <http://127.0.0.1:28190>，填上游 NUT Server 地址、UPS 名、账号密码；
2. 点「测试连接」确认能读到 UPS；
3. 保持**演练模式**开启，做一次真实断电演练，对着日志确认判定；
4. 确认无误后再关演练模式。

## Windows 版注意点

| 项 | 说明 |
|---|---|
| 默认关机命令 | `shutdown /s /t 0`（界面「关机命令」可改，留空用平台默认） |
| 关机权限 | SYSTEM 计划任务天然有；交互运行时当前用户默认也有关机权限 |
| 局域网访问管理界面 | 防火墙规则由安装脚本创建；若改了端口需自行补规则 |
| 通信中断 | 与 fnOS 版一致：stale 只报警不关机 |
| 演练模式 | 默认开启，先演练再实跑 |

## 常见问题

**界面打不开** —— `schtasks /Query /TN upsbridge` 看任务是否存在，`type C:\ProgramData\upsbridge\upsbridge.log` 看日志；端口被占时任务起不来，先改 `--port` 或释放端口。

**触发了但没关机** —— 看日志里「关机命令」执行记录；确认没停在演练模式，确认 `shutdown /s /t 0` 手动能执行。
