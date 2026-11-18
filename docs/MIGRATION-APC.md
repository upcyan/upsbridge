# 换机方案：APC BK650M2-CH + 绿联 + 飞牛，断电同步关机

> 2026-11-16 · 目标：换 APC 后，断电时绿联和飞牛都安全关机。**范围只含关机，不含来电开机。**
>
> 配套阅读：`docs/RISK-APC-BK650M2-CH.md`（剩余风险、续航实测、等待时间该设多少）。

## 0. 先更正我之前的两个错误

**错误一：我说「绿联官方没有公布任何第三方 UPS 型号」——错的。**

我抓 `compatible-detail/id-22.html?category=5` 时没选机型，页面默认渲染「暂无数据，敬请期待...」，我就当成了结论。
那张页面是动态查询，必须先选型号（如 DH4300 Plus）+ 配件类型（UPS不间断电源）再点查询。实际结果里有：

| 品牌 | 型号 |
|---|---|
| 绿联 | US3000 |
| 绿联 | US7000 |
| 宇泰 | YTB600 Pro |
| **施耐德** | **BK650M2-CH** |

**BK650M2-CH 在绿联官方兼容列表上，且是在 DH 系列机身下查到的。** 这就把上一版方案里唯一的未知项（绿联认不认 APC）消掉了。

**错误二（更早那次）：我说绿联 NUT Server 是「只读广播」——也是错的。**

当时用 `upsmon/secret`、`admin/root` 猜密码，全被拒，我就推成「不支持认证」。真实凭据是 `nut` / `nut`：

    LOGIN ups0  -> OK
    MASTER ups0 -> MASTER-GRANTED
    FSD ups0    -> FSD-SET
    SET VAR / INSTCMD -> OK

两次错误的共同点：**从「我拿到的结果」直接跳到「功能不存在」**。一次是页面默认态，一次是密码猜错。

## 1. 结论

    APC BK650M2-CH --USB--> 绿联 <UGREEN_HOST> (usbhid-ups + upsd, primary)
                                  |  3493/tcp
                                  v
                            飞牛 <UPS_HOST> (upsmon, secondary)

**绿联当 primary，飞牛当 secondary。** 理由：

- 绿联官方兼容 BK650M2-CH，插上识别这一步没有风险；
- 绿联的 upsd 已被实测证明是完整实现（MASTER-GRANTED / FSD-SET 都通），凭据和 upsname 都是固定已知值 `nut/nut` + `ups0`；
- 绿联 NUT Server 与「连接 UPS」是 UI 开关，不碰配置文件；
- 飞牛侧虽然有 NUT 客户端二进制，但 UI 只提供「网络 UPS 服务器」（给别人连），不提供「连别人的 UPS」。这一侧必须手工配，反而更适合做 secondary。

## 2. 为什么飞牛必须是手工配

飞牛 1.2.1011 的 `trim_sac` 里，`/etc/nut/nut.conf` 的 MODE 只会写两个值：`netserver` 和 `release`。
没有 `netclient` 分支（二进制里只有 man 手册的注释提到 netclient）。UI 的 `ups` 命名空间下有：

    enableNetworkUpsServer       = 启用网络 UPS 服务器
    allowedDevices               = 允许的设备      <- 哪些设备能连我
    upsmonSecretConnection       = 使用 upsmon/secret 连接本设备

全是「服务器」侧的词。找不到任何「连接到其他设备的 UPS」「填写主设备 IP」的字段。
所以飞牛当 secondary 只能手写 `/etc/nut/upsmon.conf`，把 MODE 设成 `netclient`。

（社区里的「电援联动 PowerLink」应用就是为补这个缺口做的：让飞牛接入上游 NUT。可用，但多一个第三方依赖。）

## 3. 绿联侧操作（零配置文件改动）

1. APC 的 USB 线插绿联 USB 口；
2. UI → UPS → UPS 类型选 `USB UPS` → 选择设备，应能选到施耐德 BK650M2-CH；
3. 关机等待规则选「断电后 N 分钟」（建议 5–10）；
4. 打开 **NUT Server** 开关。

绿联不需要知道飞牛的存在。它只做一件事：把 UPS 状态挂在 3493 上广播，低电量时发 FSD。

有个价值很高的细节：绿联 compat 列表把 APC 和宇泰放在一起，说明它的 UPS 模块不只认自家 DC UPS，而是走通用 HID 路径。
（对照：现在这台 US3000 的 `driver.version.data` 是 `TOP HID 1.0`。换成 APC 后应该变成 `APC HID 0.98` 之类，可以据此确认走的是通用分支。）

## 4. 飞牛侧配置（secondary，需 root）

### 4.1 /etc/nut/nut.conf

    MODE=netclient

只起 upsmon，不起 driver 和 upsd。飞牛不读任何 UPS 硬件。

### 4.2 /etc/nut/upsmon.conf

    RUN_AS_USER nut

    MONITOR ups0@<UGREEN_HOST> 1 nut nut secondary
    MINSUPPLIES 1

    SHUTDOWNCMD "/sbin/shutdown -h +0"

    POLLFREQ 5
    POLLFREQALERT 5
    HOSTSYNC 60
    DEADTIME 15
    MAXRETRY 3
    MAXAGE 15
    FINALDELAY 0

四个参数的意思：

- `secondary`：不当 primary。同一个 UPS 只能有一个 primary，绿联已经占了；
- `FINALDELAY 0`：收到 FSD 立刻关机，不等。飞牛没有需要优雅收尾的 UPS 硬件，越早关越省电；
- `HOSTSYNC 60`：等不到绿联的 FSD 时，最多等 60s 也自己关（防绿联先挂）；
- `DEADTIME 15` / `MAXAGE 15`：网络断了 15s 才算 stale，**只报警不关机**——断网不该触发关机。

### 4.3 关掉飞牛 UI 里的 UPS 支持

飞牛 UI 的「启用 UPS 支持」如果开着，`trim_sac` 会重写 `/etc/nut/*`（MODE=netserver），把手写配置冲掉。
**必须保持关闭**，让 `/etc/nut` 归手工管理。

这是本方案唯一的持续性风险：系统升级或误点 UI 都可能覆盖配置。
缓解：把上面两份配置留一份在 `/root/nut-backup/`，并在验证清单里加一条「升级后复查」。

## 5. 关机时序

    市电断
      |
      +--> 绿联收到 OB（UPS 电池供电）
      |
      +--> 绿联按规则等待 N 分钟，或电池到低电量门限
      |
      +--> 绿联广播 FSD  ------------> 飞牛 upsmon 收到 FSD
      |                                   |
      |                                   +--> FINALDELAY 0，立刻 shutdown -h +0
      |                                   |
      |                                   +--> upsmon 断开与绿联的连接
      |
      +--> 绿联等 HOSTSYNC（看到飞牛断开，或超时）
      |
      +--> 绿联执行自己的关机
      |
      +--> （可选）「根据规则关机时同步通知 UPS 关闭电源」-> APC 切断输出

顺序是对的：飞牛先关，绿联后关。因为 primary 必须活到把 FSD 发出去并确认 secondary 下线。

## 6. 换机后会碰到的问题

### 6.1 APC 的假低电量（必踩）

飞牛论坛同型号实测：`battery.charge: 98`，`battery.charge.low: 96`——差 2%，NUT 判低电量，断电极短时间就关机。
APC 的 `charge.low` 是校准告警点，不是关机门限，NUT 按关机门限来读就错了。

反证：**绿联自己给 US3000 设了 `driver.flag.ignorelb: enabled`**，说明绿联的 UPS 模块已经踩过并处理过这个坑。
所以换 APC 后大概率不用管——但我仍然建议换机后读一次：

    upsc ups0@<UGREEN_HOST> | grep -E 'ignorelb|charge.low|charge:'

如果 `ignorelb` 不是 enabled 且 `charge.low` 接近满电值，就得在绿联侧的驱动配置里加（绿联可能不给这个入口，那时只能接受「断电几分钟就关」——对 NAS 反而安全）。

### 6.2 NUT 2.8 可能管不住 APC 断电

Chiphell 实机帖：NUT 2.8 对 BK650 系列发关机指令报错，2.7.4 才正常。
这条**不影响同步关机**（关机是 upsmon 做的，跟驱动无关），只影响「UPS 自己要不要断电」。
既然你的需求只到关机，这条可以先不管。

### 6.3 切换是毫秒级，不是秒级（已更正）

早先这里写「0.6–1s 真断电」——数字来源是经销商页面，与官方说明书矛盾，已按官方 PDF 更正：

- 官方说明书：**转换时间 6ms（典型）/ 10ms（最长）**；
- 该经销商页同段还把拓扑写成「在线互动式」，而官方 PDF 与其它商家规格都指向**后备式**，属页面模板串行。

US3000 是 DC UPS 直供无中断，APC 是继电器切换，但中断只有毫秒级，两台 NAS 的电源都扛得住。
真实拔电演练仍是唯一判据（APC 页面数据向来不可靠）。

剩余风险与时间预算见 `docs/RISK-APC-BK650M2-CH.md`。

### 6.4 插口只有 4 个

两台 NAS + 交换机 + 路由器 = 4 个，刚好满。**交换机/路由器必须接 UPS**，否则断电后局域网先断，飞牛收不到 FSD，整个方案失效。

## 7. 验证清单

换机前（不用等 UPS 到）：

- [ ] 确认交换机、路由器都能上 APC 的 4 个口；不够就先买插排
- [ ] 备份飞牛 `/etc/nut/*` 到 `/root/nut-backup/`

换机后：

- [ ] 绿联 UI 识别出施耐德 BK650M2-CH
- [ ] `upsc ups0@<UGREEN_HOST>` 能看到 `device.mfr: APC`，且 `battery.charge` 有真实数值
- [ ] 检查 `driver.flag.ignorelb` 是否为 enabled（见 6.1）
- [ ] 飞牛 `upsmon` 日志出现 `Communications with UPS ups0@<UGREEN_HOST> established`
- [ ] 绿联侧能看到飞牛的连接（`LIST CLIENT ups0`）
- [ ] **拔市电演练**：两台都进 OB；绿联发 FSD 后飞牛先关，绿联后关
- [ ] **只断网演练**：拔飞牛的网线，等 1 分钟，飞牛不应该关机（只报警）
- [ ] 系统升级后复查 `/etc/nut/upsmon.conf` 是否被 trim_sac 覆盖

## 8. 一句话

绿联官方兼容 BK650M2-CH，绿联当 primary 零改动；飞牛 UI 没有客户端入口，手工把 `/etc/nut` 改成 `netclient` 指向 `ups0@<UGREEN_HOST>` 即可。
唯一需要长期留意的是飞牛的 `trim_sac` 会覆盖这份手工配置。
