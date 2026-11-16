# upsbridge — fnOS UPS 断电联动

飞牛 fnOS 第三方应用：接入局域网里**任意** NUT Server（绿联 / 群晖 / 威联通 / 树莓派 / 飞牛），
市电中断后按规则让这台 NAS 自动关机。**不修改 `/etc/nut`**。

> 只负责断电关机，不负责来电开机。

## 为什么需要它

飞牛 fnOS（验证版本 1.2.1011）的 UPS 设置只有「当服务器」这一侧：

- `trim_sac` 写 `/etc/nut/nut.conf` 时 `MODE` 只会是 `netserver` 或 `release`，**没有 `netclient` 分支**；
- UI 的 `ups` 命名空间里只有 `enableNetworkUpsServer`（给别人连）、`allowedDevices`、
  「使用 upsmon/secret 连接本设备」这类服务器侧词条，**没有「连接其他设备 UPS」的入口**。

所以一台 UPS 想保护两台 NAS 时，飞牛这一侧系统不给做。手工改 `/etc/nut` 可行，但 `trim_sac` 会覆盖它——
改 UPS 设置、系统升级都可能被冲回去。

本应用换了个做法：不碰 `/etc/nut`，自己实现 NUT 协议客户端，配置全放在应用自己的目录里。

## 它做什么

```
         UPS ──USB──> 任意一台机器（NUT Server，3493/tcp）
                              │
                              ▼  局域网
                        本应用（NUT Client）
```

| 能力 | 说明 |
|---|---|
| 轮询上游 | 默认 5s，读 `ups.status` / `battery.charge` / `battery.runtime` / `device.mfr` + `model` |
| 断电时长判定 | 市电中断持续 N 秒后关机（N 可配，0 = 关闭） |
| 低电量判定 | 收到 `LB`，或电量低于自定义阈值 |
| 强制关机 | 收到上游广播的 `FSD` |
| 通信中断 | 按 `deadtime` 判为 stale，**只报警不关机** |
| 演练模式 | 默认开启，触发只写日志，不真的关机 |
| 管理界面 | 状态卡、上游连接测试、规则编辑、日志实时刷新 |

## 安装

1. 到 [Releases](../../releases) 下载 `upsbridge_<version>.fpk`；
2. 飞牛「应用中心 → 右上角手动安装」选中该 fpk；
3. 装好后从桌面打开「UPS 断电联动」。

命令行安装也可以（需要先 `trim-cli login` 完成设备授权，否则会报 `saved session is required`）：

```bash
trim-cli app install-fpk /path/to/upsbridge_1.0.0.fpk --accept-license --yes
```

## 配置

打开应用，填上游 NUT Server 的连接信息，点「测试连接」。

各家默认标识（**以你自己设备上实际配置为准**，可用 `upsc -l <主机>` 查看 UPS 名称）：

| 上游设备 | UPS 名称 | 账号 | 密码 |
|---|---|---|---|
| 绿联 UGOS Pro | `ups0` | `nut` | `nut` |
| 群晖 DSM | `ups` | `monuser` | `secret` |
| 威联通 QNAP | `qnapups` | `admin` | `123456` |
| 飞牛 fnOS | `ups` | `monuser` | `secret` |

### 关机规则

| 字段 | 默认 | 说明 |
|---|---|---|
| `shutdown_on_battery_seconds` | 300 | 断电持续多少秒后关机，0 = 不按时间触发 |
| `shutdown_on_lowbattery` | true | 收到 `LB` 时关机 |
| `shutdown_on_fsd` | true | 收到上游 `FSD` 时关机 |
| `battery_charge_low_override` | 0 | 电量低于此值视为低电量，0 = 不启用 |
| `deadtime` | 30 | 通信中断多少秒判为 stale（不触发关机） |
| `poll_interval` | 5 | 轮询间隔（秒） |
| `shutdown_cmd` | `/sbin/shutdown -h +0` | 触发时执行的命令 |
| `fsd_grace_seconds` | 15 | `FSD` 需持续多久才动作（见下） |
| `dry_run` | **true** | 演练模式：只记日志不关机 |

## 首次使用建议

1. 保持演练模式开启，点「测试连接」确认能读到 UPS；
2. 做一次真实断电演练（拔插排），对着日志确认判定符合预期；
3. 确认无误后再关掉演练模式。

## 两个已踩过的坑

### 上游残留的 FSD

NUT 没有「清除 FSD」的命令——发过一次 `FSD` 后，上游 `ups.status` 会一直带着 `FSD`，
只有重启上游 `upsd` 才能清掉。直接信任它会导致本应用一启动就误关机。

为此做了两道闸：

1. `fsd_grace_seconds`（默认 15s）：`FSD` 必须持续超过这个时长才动作；
2. **本次运行期间必须曾观测到断电（`OB`）**，才承认 `FSD` 是本次断电的信号；
   否则视为陈旧标记，只记一行日志：`上游有 FSD 标记，但本次运行未观测到断电，视为陈旧标记，不动作`。

### 假低电量

部分 APC 机型（如 BK650M2-CH）的 `battery.charge.low` 是校准告警点而非关机门限，
可能出现 `charge: 98` / `charge.low: 96`，NUT 判低电量于是一断电就关。
对策是让上游驱动加 `ignorelb`（绿联的实现默认就带），并用本应用的
`battery_charge_low_override` 或「断电时长」代替百分比判定。

## 目录

```
app/                    fpk 源（manifest / cmd / config / ui / target）
  target/upsbridge/upsbridge.py   主程序，零第三方依赖
  target/upsbridge/config.default.json
docs/                   调研记录与方案对比
examples/              NUT primary/secondary 配置样例与关机脚本
tools/pack.sh          打包 -> dist/upsbridge_<version>.fpk
tools/mockupsd.py      可控假 upsd，用于本地测触发逻辑
tools/mkicon.py        生成应用图标 PNG
```

## 从源码打包

```bash
bash tools/pack.sh          # 产出 dist/upsbridge_<version>.fpk
```

`.fpk` 是 tar.gz，外层含 `manifest` / `app.tgz` / `cmd` / `config` / `ui` / 图标；
`app.tgz` 解压后落到 `target/`。**应用文件必须打成 `app.tgz`**，直接把 `target/` 放进 fpk 会报
「解压app.tgz失败」。

## 本地测触发逻辑

```bash
python3 tools/mockupsd.py            # 起一个假 upsd（默认 13493）
# 用 stdin 控制：status=OB / status=OL / fsd=1 / fsd=0
```

## 状态

- 目标平台：fnOS 1.2.x / x86_64
- 依赖：系统自带 `python3`（3.11+），无第三方包
- 权限：`run-as: root`（要执行 shutdown）

## License

MIT，见 [LICENSE](LICENSE)。