## upsbridge 1.0.1

修复一个会让断电保护**静默失效**的缺陷。

### 修了什么

**残留 pidfile 挡住启动。** 上一版 `cmd/main status` 只判 `/proc/$pid` 是否存在。系统重启后 pid 会被别的进程复用，残留的 pidfile 于是指向一个完全无关的程序（本机实测指向 `trim-preview-server`）。后果是应用中心开机拉起时，`cmd/main start` 认为「已在运行」直接返回 0，实际进程根本没起来 —— 而日志里只有一行 `already running`，看起来一切正常。

现在 `is_running()` 会核对 `/proc/$pid/cmdline` 里确实有 `upsbridge.py`，归属不符就删掉脏 pidfile 重来；`start` 也改成先探活再写 pidfile，启动失败不留脏文件。

```
# 事故现场
/var/log/trim_app_center/info.log:
08:22:43 msg="already running" appName=upsbridge class=start code=0   # 实际没拉起
```

**`OB` / `LB` 改成 token 精确匹配。** 原来用子串判断，`OL RB` 这种组合状态里含 `RB` 不含 `OB` 尚可，但 `OB DISCHRG` / `LB DISCHRG` 之外再多一个词就可能误命中。现在统一 `set(status.split())` 比较。

### 加了什么

**`ups.status` 变化写入日志。** 只记变化不记每次轮询，并忽略 `CHRG` 抖动 —— APC Back-UPS 接市电时 `OL` 与 `OL CHRG` 会反复跳（本机 60 秒内切了 26 次），把这种抖动也记下来会刷满日志、淹没真正要复盘的 `OB` / `LB` / `OVER`。

**`RB` 只记录不关机。** 上游报 RB 时写一行「建议更换电池」，不参与任何关机判定。

```
UPS 状态：OL CHRG
UPS 状态变化：OL → OL RB
上游报告 RB（建议更换电池）：本机不据此关机，仅记录
UPS 状态变化：OL RB → OB DISCHRG
检测到市电中断，开始计时（阈值 300s）
```

### 升级

应用中心手动安装 `upsbridge_1.0.1.fpk` 覆盖即可，配置（`/vol1/@appconf/upsbridge/config.json`）保留。

装完看一眼状态卡是不是「运行中」，再确认日志里有新的一行 `UPS 状态：...` —— 这行是新版本才有的。
## upsbridge 1.0.0

飞牛 fnOS 应用：接入局域网内**任意** NUT Server，市电中断后按规则让本机自动关机。
**不修改 `/etc/nut`**，配置全在应用目录，系统升级不会被 `trim_sac` 冲掉。

### 为什么需要它

fnOS 1.2.1011 的 UPS 设置只有「当服务器」这一侧：`nut.conf` 的 `MODE` 只会写 `netserver`/`release`，
没有 `netclient` 分支；UI 里也没有「连接其他设备 UPS」的入口。一台 UPS 保护两台 NAS 时，飞牛这侧系统不给做。

### 功能

- 自实现 NUT 协议客户端（USERNAME / PASSWORD / LOGIN / LIST VAR），零第三方依赖
- 四条触发规则：断电持续 N 秒 · 收到 `LB` · 电量低于阈值 · 上游广播 `FSD`
- 通信中断按 `deadtime` 判为 stale，**只报警不关机**
- 默认**演练模式**：触发只写日志，不真的关机
- 网页面板：状态卡、上游连接测试、规则编辑、日志实时刷新（2s）

### 两个已踩过的坑

**上游残留 FSD**：NUT 没有清除 FSD 的命令，发过一次后 `ups.status` 会一直带 `FSD`，只有重启上游 `upsd` 才清。
为此加了两道闸 —— `fsd_grace_seconds`（默认 15s）要求 FSD 持续，且**本次运行必须曾观测到 `OB`** 才承认它。
两种情况都实测过：陈旧 FSD 只记日志不动作，真实断电时的 FSD 照常触发。

**假低电量**：部分 APC 机型（如 BK650M2-CH）的 `battery.charge.low` 是校准告警点而非关机门限，
可能出现 `98`/`96` 这种，一断电就判 LB。可用 `battery_charge_low_override` 或「断电时长」代替百分比判定。

### 安装

下载下方 `upsbridge_1.0.0.fpk` → 飞牛「应用中心 → 手动安装」选中它。

### 首次使用

1. 打开应用，填上游地址（绿联 `ups0`/`nut`/`nut`，群晖 `ups`/`monuser`/`secret`），点「测试连接」
2. **保持演练模式**，做一次真实断电演练，对着日志确认判定符合预期
3. 确认无误后再关掉演练模式

### 已实测

| 场景 | 结果 |
|---|---|
| 正常轮询 | 读到上游 UPS 型号、电量、续航 |
| 断电未达阈值 | 不触发 |
| 断电超过阈值 | 演练触发并记录原因 |
| 恢复市电 | 计时归零 |
| 上游 FSD（观测过 OB 后） | 演练触发 |
| 上游残留 FSD（未观测 OB） | 视为陈旧标记，不动作 |
| 上游进程被杀 | stale，不关机；恢复后自动清除 |

MIT License。