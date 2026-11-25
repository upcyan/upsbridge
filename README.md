# upsbridge — fnOS UPS 断电联动

飞牛 fnOS 第三方应用：接入局域网里任意 NUT Server，市电中断后让这台 NAS 按时关机。配置全在应用自己的目录里，不碰 `/etc/nut`。只负责断电关机，不负责来电开机。

```
UPS ──USB──> 某台机器（NUT Server，3493/tcp）
                   │  局域网
                   ▼
              upsbridge（NUT 客户端）
```

## 安装

从 [Releases](../../releases) 下载 `upsbridge_<version>.fpk`，应用中心右上角手动安装，装完从桌面打开。

## 配置

打开应用填上游信息，点「测试连接」。各家默认标识（以自己设备为准）：

| 上游设备 | UPS 名称 | 账号 | 密码 |
|---|---|---|---|
| 绿联 UGOS Pro | `ups0` | `nut` | `nut` |
| 群晖 DSM | `ups` | `monuser` | `secret` |
| 威联通 QNAP | `qnapups` | `admin` | `123456` |
| 飞牛 fnOS | `ups` | `monuser` | `secret` |

密码框留空表示不修改。保持演练模式（默认开启），拔一次插排做断电演练，对着日志确认判定符合预期，再关掉演练模式。

### 关机规则

| 字段 | 默认 | 说明 |
|---|---|---|
| `shutdown_on_battery_seconds` | 300 | 断电多少秒后关机，0 = 不按时间触发 |
| `shutdown_on_lowbattery` | true | 收到 `LB` 时关机 |
| `shutdown_on_fsd` | true | 收到 `FSD` 时关机 |
| `battery_charge_low_override` | 0 | 电量低于此值视为低电量，0 = 不启用 |
| `shutdown_when_stale_seconds` | 0 | 失联多久后关机，0 = 不启用 |
| `deadtime` | 30 | 通信中断多少秒判为 stale |
| `poll_interval` | 5 | 轮询间隔（秒） |
| `witness_host` | 空 | 诊断时的旁路目标，不能填 NUT 服务器自己 |
| `dry_run` | **true** | 演练模式，只记日志 |

## 权限模型

`cmd/main`（root）起一个只认 `ping` 和 `poweroff` 的白名单助手，前台服务降权到 `upsbridge` 用户运行。助手走 unix socket + `SO_PEERCRED` 校验。缺用户或缺 `setpriv` 时退回 root 直跑，功能不受影响。Web 默认只听 `127.0.0.1`，桌面经 fnOS 网关代理；要局域网直连需设 `server.bind: 0.0.0.0` 和 `server.token`。

## 其他平台

| 平台 | 入口 | 默认关机命令 | 配置位置 |
|---|---|---|---|
| fnOS | fpk 安装 | `/sbin/shutdown -h +0` | `/var/apps/upsbridge/etc/config.json` |
| Windows | [windows/README.md](windows/README.md) | `shutdown /s /t 0` | `%APPDATA%\upsbridge\` |
| Docker | [docker/README.md](docker/README.md) | `nsenter -t 1 …` | 挂载卷 `docker/data/` |

## 开发

```bash
bash tools/pack.sh             # 产出 dist/upsbridge_<version>.fpk
python3 tools/test_lb_guard.py # 时序演练，走假 upsd
python3 tools/mkicon.py        # 从 icon.svg 重渲染应用图标
```

MIT，见 [LICENSE](LICENSE)。
