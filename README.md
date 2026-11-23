# upsbridge — fnOS UPS 断电联动

飞牛 fnOS 第三方应用：接入局域网里任意 NUT Server，市电中断后让这台 NAS 按时关机。配置全在应用自己的目录里，不碰 `/etc/nut`。

只负责断电关机，不负责来电开机。

## 为什么会有这个应用

飞牛的 UPS 设置只做了「当服务器」这一侧：`trim_sac` 写 `/etc/nut/nut.conf` 时 `MODE` 只有 `netserver` 或 `release`，没有 client 分支，界面上也没有「连接别的设备的 UPS」的入口。一台 UPS 要保护两台 NAS，系统这一层就不给做。

手工改 `/etc/nut` 能work，但改 UPS 设置或系统升级时会被 `trim_sac` 冲回去。所以这里换个做法：自己实现 NUT 客户端，只读上游，不写系统配置。

## 能做什么

```
UPS ──USB──> 某台机器（NUT Server，3493/tcp）
                   │  局域网
                   ▼
              upsbridge（NUT 客户端）
```

| 能力 | 说明 |
|---|---|
| 轮询上游 | 默认 5 秒，读 `ups.status` / `battery.charge` / `battery.runtime` / 型号 |
| 断电时长 | 中断持续 N 秒后关机（可配，0 = 关闭） |
| 低电量 | 收到 `LB`，或电量低于自定义阈值 |
| 强制关机 | 上游广播 `FSD` |
| 通信中断 | 判为 stale，只提示不关机 |
| 旁路验证 | 上游失联时 ping 一台市电侧设备，区分网络故障还是真断电 |
| 演练模式 | 默认开启，触发只写日志 |
| 运行日志 | `/log` 子页：保留 2000 条并落盘，四色分级，可搜索、筛选、导出 |
| 事件记录 | 每次断电的起止、最低电量、结束原因，落盘 `events.jsonl` |
| 通知 | Webhook（JSON POST），可单独测 |

## 安装

从 Releases 下载 `upsbridge_<version>.fpk`，应用中心右上角手动安装，装完从桌面打开。

命令行：

```bash
trim-cli app install-fpk /path/to/upsbridge_1.3.0.fpk --accept-license --yes
```

## 配置

打开应用填上游信息，点「测试连接」。

各家默认标识（以自己设备为准，`upsc -l <主机>` 可查 UPS 名称）：

| 上游设备 | UPS 名称 | 账号 | 密码 |
|---|---|---|---|
| 绿联 UGOS Pro | `ups0` | `nut` | `nut` |
| 群晖 DSM | `ups` | `monuser` | `secret` |
| 威联通 QNAP | `qnapups` | `admin` | `123456` |
| 飞牛 fnOS | `ups` | `monuser` | `secret` |

密码框留空表示不修改，已保存的密码不会回填到页面上。

### 关机规则

| 字段 | 默认 | 说明 |
|---|---|---|
| `shutdown_on_battery_seconds` | 300 | 断电多少秒后关机，0 = 不按时间触发 |
| `shutdown_on_lowbattery` | true | 收到 `LB` 时关机 |
| `shutdown_on_fsd` | true | 收到 `FSD` 时关机 |
| `battery_charge_low_override` | 0 | 电量低于此值视为低电量，0 = 不启用 |
| `deadtime` | 30 | 通信中断多少秒判为 stale |
| `poll_interval` | 5 | 轮询间隔（秒） |
| `shutdown_cmd` | `/sbin/shutdown -h +0` | 触发时执行，留空用平台默认 |
| `fsd_grace_seconds` | 15 | `FSD` 需持续多久才动作 |
| `lb_grace_seconds` | 30 | `LB` 需持续多久才动作 |
| `witness_host` | 空 | 旁路验证的 ping 目标 |
| `dry_run` | **true** | 演练模式，只记日志 |

### 访问与鉴权

默认只监听 `127.0.0.1`，桌面打开走本机代理，不需要额外配置。

要把 `server.bind` 改成 `0.0.0.0` 给局域网直连，就必须同时设 `server.token`，否则非本机请求一律 401。请求带上 `X-Upsbridge-Token` 头或 `Authorization: Bearer <token>`。

## 开始用之前

1. 保持演练模式，点「测试连接」确认能读到 UPS；
2. 拔一次插排做断电演练，对着日志确认判定符合预期；
3. 确认无误再关掉演练模式。

## 踩过的坑

### 上游残留的 FSD

NUT 没有清除 FSD 的命令，发过一次之后 `ups.status` 会一直带着它，只有重启上游 `upsd` 才能清。直接信任会导致一启动就误关机。

两道闸：`fsd_grace_seconds` 要求 FSD 持续一段时间；本次运行必须曾观测到 `OB` 才承认它是本次断电的信号。否则只记一行日志。

### 瞬时假低电量

部分 APC / UGREEN 的 HID 驱动在 `CHRG` 切换时会瞬时把 `LB` 位置起来，表现是市电正常却报 `OL LB`，同时 `battery.charge` 还是 100%。本机实测一天抖十几次。

1.0.1 及更早的版本见到 `LB` 就关机，于是在 2026-11-17 和 11-18 两次在市电正常的情况下关机。现在 `LB` 要同时满足：确实在电池供电；电量或续航至少一个真的低于阈值；持续 `lb_grace_seconds`。被挡下时日志会写清是哪一道闸。

部分机型的 `battery.charge.low` 是校准告警点而非关机门限（如 `charge: 98` / `charge.low: 96`），可用 `battery_charge_low_override` 覆盖。

## 权限模型

```
cmd/main（root）
  ├─ 起 root 助手 privileged.py  → 只做 ping 与 poweroff，白名单两条命令
  └─ 起前台服务（降权到 upsbridge 用户）→ Web、配置、日志都没有 root 权限
```

助手只监听 unix socket，权限 0660，连接时用 `SO_PEERCRED` 核对对端 uid，ping 目标做格式校验并禁止 `-` 开头。助手脚本的副本放在 `etc/priv/`（root 700），不从应用目录直接执行——否则能改应用目录的人就能改一段以 root 跑的代码。

没有 `upsbridge` 用户或没有 `setpriv` 时自动退回 root 直跑，功能不受影响。

## 目录

```
app/                          fpk 源
  target/upsbridge/upsbridge.py    主程序，零第三方依赖
  target/upsbridge/privileged.py   root 特权助手
  cmd/main                         生命周期脚本
docs/                         调研与风险记录
windows/                      Windows 版启动脚本与自启
docker/                       Dockerfile 与 compose
examples/                     NUT 配置样例
tools/pack.sh                 打包
tools/test_lb_guard.py        低电量三道闸的时序演练
```

## 其他平台

| 平台 | 入口 | 默认关机命令 | 配置位置 |
|---|---|---|---|
| fnOS | fpk 安装 | `/sbin/shutdown -h +0` | `/var/apps/upsbridge/etc/config.json` |
| Windows | [windows/README.md](windows/README.md) | `shutdown /s /t 0` | `%APPDATA%\upsbridge\` |
| Docker | [docker/README.md](docker/README.md) | `nsenter -t 1 …` | 挂载卷 `docker/data/` |

关机命令可用环境变量 `UPSBRIDGE_SHUTDOWN_CMD` 覆盖。

## 打包与测试

```bash
bash tools/pack.sh            # 产出 dist/upsbridge_<version>.fpk
python3 tools/test_lb_guard.py # 时序演练，走假 upsd
```

fpk 是 tar.gz，外层含 `manifest` / `app.tgz` / `cmd` / `config` / `ui` / 图标。应用文件必须打成 `app.tgz`，直接把 `target/` 放进去会报「解压app.tgz失败」。

## 状态

- 平台：fnOS 1.2.x / x86_64；Windows 10+；任意 Linux（Docker）
- 依赖：系统 `python3` 3.11+，无第三方包
- 权限：root（起停与特权助手），业务进程降权运行

## License

MIT，见 [LICENSE](LICENSE)。
