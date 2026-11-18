# 换 APC 后的剩余风险与我方参数该设多少

> 2026-11-18 · 承接 MIGRATION-APC.md。只说还没解决的事，已定案的不重复。
> 范围仍然是：断电时两台 NAS 都安全关机，不涉及来电开机。

## 0. 一句话结论

架构没问题，风险全在**时间预算**和**顺序依赖**上：
飞牛这套客户端（upsbridge）在「与上游失联」时的策略是**不关机**，而绿联关机时会把 NUT Server 一起带走。
所以必须保证**飞牛先关、绿联后关**，两者等待时间差至少要留 1 分钟以上余量。
剩下的都能扛。

## 1. 电池到底能撑多久（决定等待时间上限）

先算插座上到底挂了多大负载：

| 设备 | 估算功率 | 依据 |
|---|---|---|
| 绿联 NAS | 25–40 W | UGREEN 实验室数据：2 盘 DH2300 13.7W，8 盘 DXP8800 Plus 70.4W |
| 飞牛 NAS | 8–28 W | 第三方横评：飞牛 Basic4 待机 8W / 满载 28W |
| 交换机 | 5–10 W | 千兆非管理型典型值 |
| 路由器 | 8–15 W | 家用 Wi-Fi 6 典型值 |
| **合计** | **约 45–95 W** | |

再看 APC 侧续航（经销商实测数据，非官方图表）：

| 负载 | 后备续航 |
|---|---|
| 79 W（笔记本+路由器） | 约 25 分钟 |
| 119 W（台式机+显示器+路由器） | 约 16 分钟 |
| 240 W（高配主机+大屏+音箱+路由器） | 约 7.5 分钟 |
| 390 W（额定满载） | 官方只给「4 分钟 1 秒」这个区间值，不可直接采信 |

按 45–95 W 内插，**保守可用续航取 20–35 分钟**（新电池；旧电池按七折）。
500 元级后备式的官方数据本来就虚，别按 25 分钟规划，**按 20 分钟画红线**。

## 2. 时间预算表（这才是要落到配置里的东西）

绿联有硬约束：主设备等待时间必须比从设备长，官方建议「从设备 ≤ 主设备 − 10 秒」；
飞牛这边是我方 upsbridge，阈值是 `shutdown_on_battery_seconds`。

推荐落点：

| 项目 | 值 | 理由 |
|---|---|---|
| 飞牛 `shutdown_on_battery_seconds` | **300**（5 分钟，维持现值） | 断电 5 分钟后再关，短暂闪断不误关 |
| 绿联「等待时间」 | **10 分钟** | 比飞牛晚 5 分钟，远超官方要求的 10 秒；即便绿联提前 1 分钟关，飞牛也已经关完 |
| 绿联「保护方式」 | 自动关机 | 待机模式下绿联不断电，NUT Server 还活着，飞牛能继续跟 |
| 飞牛 `deadtime` | 60（维持现值） | 断网/上游消失只报警不关机 |

**如果绿联固件只能设到 5 分钟**（老固件下限是 5 分钟，2026-01 版才放开到 1 分钟，见 §4.2），
那就不改绿联、改飞牛：

| 项目 | 值 |
|---|---|
| 飞牛 `shutdown_on_battery_seconds` | **180**（3 分钟） |
| 绿联等待时间 | 5 分钟（下限，不能再降） |

两种配置都远小于 20 分钟的电池红线，安全。

## 3. 第一个真风险：绿联先关，飞牛就不关了

这是整套方案里**唯一会导致飞牛硬断电**的路径：

    市电断 → 双方都进 OB
      → 绿联等 5 分钟先关机（假设绿联设得太短）
      → 绿联关机时 NUT Server 一起没了
      → 飞牛连不上 192.168.68.53:3493
      → 飞牛的策略是「失联不关机」，只记一条 warn
      → 电池耗尽，飞牛硬断电

这不是理论推演，是 upsbridge 的既定行为：
`app/target/upsbridge/upsbridge.py:337-339` 判 stale 时日志明写「不触发关机」，
`deadtime` 只是把「连不上」升级成「标记 stale」，从不会转成关机动作。

**所以 §2 的时间差不是调优，是正确性条件。** 拔电演练时必须验证顺序：
先看到飞牛 app.log 里的「触发条件满足」，再看到绿联关机。

## 4. 待验证项（换机后必须实测，不能靠推测）

### 4.1 绿联会不会真的广播 FSD

标准 NUT 里，primary 判 critical 后在 upsd 上设 FSD，secondary 轮询时看到。
飞牛论坛与绿联官方文档都只说「从设备通过轮询主设备的 UPS 状态自主触发关机」，
没明说绿联的 upsd 是否实现 FSD。

我方 upsbridge 不依赖 FSD 也能工作（自己数 OB 时长），FSD 只是加速通道，且有两道闸：
必须本次运行期间见过 OB、必须持续 `fsd_grace_seconds`（15s）。
**验证方法**：拔电后在拔掉飞牛网线之前，抓一次 `LIST VAR ups0`，看有没有 `FSD` 行。

### 4.2 绿联等待时间的下限

- 老固件：填 2 分钟会提示「最小只能设置 5 分钟」；
- 2026-01 UGOS Pro 更新日志：「待机模式和自动关机，自定义时间设置的最小值调整为 1 分钟」。

换机后先看一眼固件版本，决定走 §2 的哪一套。

### 4.3 APC 的假低电量：怪谁？

同型号实测：`battery.charge 98` / `battery.charge.low 96`，差 2% 就判 LB。
绿联给自家的 US3000 设了 `driver.flag.ignorelb: enabled`，说明他们也踩过。
换 APC 后请读一次：

    upsc ups0@192.168.68.53 | grep -E 'ignorelb|charge.low|battery.charge:'

**是谁的问题：三者都不是，但责任能分清。**

排除电池。本机同时刻读数：`battery.charge 100`、`battery.voltage 13.5`
（标称 12V 满充）、`battery.runtime 2403s`、`ups.test.result "Done and passed"`、
`ups.mfr.date 2026/07/24`（四个月前出厂）。一条都不指向老化。
`battery.mfr.date` 曾经读到 2001/01/01 —— 那是寄存器里写 0，
被 `apc-hid.c:109 apc_date_conversion_fun` 反解成 2001，不是"电池 2001 年生产"。

不是绿联的自研 bug。绿联 NUT Server 报 `driver.version 2.8.0`、
`driver.version.data "APC HID 0.98"`，就是**原版 NUT 2.8.0 的 usbhid-ups** 在跑，
一行没改。它转发 LB 是因为 NUT 2.8.0 本来就没有任何防抖机制（见下）。

是 APC 固件瞬时置位 + NUT 旧版本没有延迟机制，两者相加。
NUT 官方 usbhid-ups 手册（`networkupstools.org/docs/man/usbhid-ups.html`）
在 `lbrb_log_delay_sec` 条目里**点名了这个型号**：

> Some APC BXnnnnMI device models or firmware versions (reportedly 2023-2024),
> or APC BVKnnnnM2 device models, or **APC BKnnnnM2-CH (a China-market-only model)**,
> frequently report "low battery", "replace battery", and "all ok" states
> changing rapidly within a couple of seconds, sometimes (but not always)
> preceded by OL+DISCHRG (presumably calibration).

即：APC 固件在 `UPS.PowerSummary.PresentStatus.BelowRemainingCapacityLimit`
这一位上会在一两秒内反复置位/复位（`apc-hid.c:380` 是 LB 的唯一来源），
NUT 把它当作"已知的厂商固件现象"来兼容，而不是当成绿联的错。

NUT 加的修复是 `lbrb_log_delay_sec`（`usbhid-ups.c` 2.8.5-1）：
设备进入 LB / LB+RB 后先不置位，持续超过 N 秒才认。
`BK…M2-CH` 匹配在一段型号自动判定里（`usbhid-ups.c:1865-1977`，条件是
vendor 为 APC 且 productLen>9 且以 `M2` + `-`/`_` + `CH` 结尾），
命中即默认 3 秒，还会建议同时加 `lbrb_log_delay_without_calibrating`。

版本对得上：Debian 的 2.8.0-7 与 2.8.1-5 的 `usbhid-ups.c` 里
grep `lbrb_log_delay` 命中 **0** 次，2.8.5-1 才有。
所以绿联这套（2.8.0）不是配错，是**根本没有这个参数**。

还有一个容易漏的坑（NUT issue #2942 里就栽在这）：
`usbhid-ups.c:2897-2921` 的判断是
`if (lbrb_log_delay_sec < 1 || (!isCalibrating && !lbrb_log_delay_without_calibrating) || !(ups_status & STATUS(ONLINE)))`
→ 市电正常时 `isCalibrating` 为假，若不显式加 `lbrb_log_delay_without_calibrating`，
延迟**照样不生效**。而且最后那项 `!(ups_status & STATUS(ONLINE))`
意味着"一旦不是在线态就不延迟"——在线位抖动的那一瞬间，LB 直接漏出去。

**能不能在绿联侧修？不能。** 绿联的 UPS 设置页没有 ups.conf 编辑入口，
`driver.flag.ignorelb` 是它给自家 US3000 预置的，用户改不了别人的驱动参数。
所以只能在客户端（飞牛 upsbridge）挡。

upsbridge 侧已有三道闸（必须在电池供电 / 数值必须真的低于阈值 / 持续 30 秒），
所以即使 APC 抖 LB，也不会像 1.0.1 那样立刻关机。这条已经从「风险」降级为「观察项」。

也可以顺手把绿联的读数拉来做交叉验证——抖的时候上游 `battery.charge`
不会真的掉下去，这就是最直接的否证。

### 4.4 NUT 2.8 能不能让 APC 断电

Chiphell 实测：NUT 2.8 对 BK650 系列发关机指令报错，2.7.4 才正常。
影响面：**只影响「UPS 自己要不要在关机后切断输出」**，不影响两台 NAS 的关机动作。
你的需求不含来电开机，所以即使这条失效也没关系——电池自然耗尽即可。

### 4.5 插口刚好够，没有余量

BK650M2-CH 是 4 个国标口，**四路全部带电池后备 + 浪涌保护**（不是「2 后备 + 2 纯防雷」那种廉价布局）。
两台 NAS + 交换机 + 路由器 = 4，正好满。
**交换机/路由器必须接 UPS**，否则断电后局域网先断，飞牛连不上绿联，整条链失效。
想再加设备只能上插排，但插排本身不增加电池容量，别接大功率。

### 4.6 充电要 24 小时

BK650M2-CH 官方标称充电 24 小时。
新机到手先充 24 小时再做拔电演练，否则测出来的续航没有参考价值；
真断电恢复后如果短时间内再断一次，第二次的续航会明显不足。

## 5. 已修正的历史错误

`MIGRATION-APC.md` §6.3 原来写「转换时间 0.6–1s，是 0.6–1 秒的真断电」——数字错了。

- 官方说明书 PDF：**转换时间 6ms（典型）/ 10ms（最长）**；
- 经销商页面写的「0.6 秒～1 秒」很可能是模板串行（同页面还把拓扑写成「在线互动式」，而官方 PDF 与商家规格都指向**后备式**）。

结论：切换中断是**毫秒级**，不是秒级。两台 NAS 的电源都扛得住，不必为此换电源。
但真实拔电演练仍是唯一判据——APC 页面数据向来不可靠。

## 6. 换机后要做的四件事（按顺序）

1. 插上 UPS，充电 24 小时。
2. 绿联 UI 确认识别到 `Back-UPS BK650M2-CH`，把等待时间设成 §2 的值。
3. 读一次 `upsc ups0@192.168.68.53`，确认 `battery.charge` 是真数值、`ignorelb` 是否 enabled。
4. 拔市电演练，按这个顺序核对：
   - 飞牛 app.log 出现「检测到市电中断，开始计时」；
   - 5 分钟后出现「触发条件满足」+「Shutdown scheduled for …」；
   - 飞牛关完之后，绿联才开始关；
   - 恢复市电，两边都能正常起来（能不能自动起来不在本次范围）。

## 7. 参考

- 绿联官方 NUT Server 教程（UGOS Pro 1.6.1.2846）：https://www.ugnas.com/play-detail/id-95.html
- 绿联繁体版同文（含等待时间设置与注意事项）：https://support.ugnas.com/detail/article/zh-TW/708
- 绿联 2026-01 更新日志（等待时间下限改为 1 分钟）：https://club.ugnas.com/forum.php?mod=viewthread&tid=2591
- 等待时间下限 5 分钟实测评帖：https://club.ugnas.com/forum.php?mod=viewthread&tid=1717
- BK650M2-CH 经销商续航实测：https://synologynas.cn/Newsinformation?article_id=2858
- UGREEN 官方 NAS 功耗实验室数据：https://ai.ugreen.com/blogs/knowledge/how-much-power-does-your-nas-consume
- NUT 关机时序（HOSTSYNC / FINALDELAY / POWERDOWNFLAG）：https://dev.to/charleshartmann/nut-shutdown-on-a-timer-upssched-secondaries-first-and-hostsync-explained-1ngj