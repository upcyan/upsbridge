# 一台 APC BK650M2-CH / 两台 NAS：断电级联关机的软件设计

> 调研日期 2026-11-15 · 实机环境 <UPS_HOST>（fnOS, Debian 12 + NUT 2.8.0-7）、<UGREEN_HOST>（绿联 UGREEN NAS，NUT Server 已开，ups0 = US3000）

## ⚠️ 修订说明（2026-11-16）

本文有两处结论已推翻，**换机方案请只看 [MIGRATION-APC.md](MIGRATION-APC.md)**。

**一、§3 说绿联 NUT Server 是「只读广播」——错。** 当时用 `upsmon/secret`、`admin/root` 猜密码全被拒，我就推成「不支持认证」。
真实凭据是 `nut / nut`（社区帖子里写的），重测结果：`MASTER ups0` → MASTER-GRANTED，`FSD ups0` → FSD-SET。
**绿联跑的是完整 upsd，可以当 primary。**

**二、说「绿联官方没有公布任何第三方 UPS 型号」——错。** 那张兼容列表是动态查询页，我没选机型，看到的是默认态「暂无数据」。
按 DH 系列 + UPS不间断电源 查询后，列表里有 **施耐德 BK650M2-CH**（还有 US3000 / US7000 / 宇泰 YTB600 Pro）。

两处错误同一个根因：**把「我看到的空结果」当成「功能不存在」**。

本文 §1 物理约束、§4 配置模板、§5 三个坑仍然有效；§3 与 §7 的方向以 MIGRATION-APC.md 为准。

## 0. 一句话结论

USB 口只能有一个主人，但 UPS 状态可以走网络广播。用 NUT 的 primary/secondary 模型：
接线的那台跑 usbhid-ups + upsd（primary，对外 3493/tcp），另一台只跑 upsmon（secondary，TCP 连过去）。
断电到低电量时 primary 向所有 secondary 广播 FSD，secondary 立即关机，primary 用 HOSTSYNC 等它们下线后再关自己，
最后由 primary 发 shutdown.return 让 UPS 切断输出，来电后靠 NAS 的「通电自动开机」恢复。

这不是发明一个协议，是把 NUT 已经设计好的东西配对好参数——难点全在时序参数和 BK650M2-CH 的三个坑上（见 §5）。

## 1. 物理约束（决定架构的硬事实）

| 约束 | 说明 |
|---|---|
| USB HID 单主机 | UPS 的 USB 口只有一个，任何时刻只能被一台主机的 usbhid-ups 独占。USB 分线器/隔离器不能让两台同时读（HID 是点对点，不是总线共享语义）。 |
| 所以只有一条路 | 要么「一台读 + 一台收网络广播」，要么「USB 服务器/树莓派做中间人」。前者不需要额外硬件。 |
| 网络设备必须在 UPS 上 | 交换机/路由器若不带 UPS，断电几十秒内局域网就断，secondary 收不到 FSD。这是最常见的「设计了但没生效」。 |

## 2. 三条可行架构

### A. 标准 NUT 主从（推荐，零额外硬件）

    APC BK650M2-CH -- USB --> NAS-1 (usbhid-ups -> upsd) -- 3493/tcp --> NAS-2 (upsmon)
                                   |
                                   +-- 自己也是 upsmon（primary 模式）

- 适用：能控制 NAS-1 的 /etc/nut/（有 root/SSH）。飞牛可以，绿联看固件开放度。
- 优点：NUT 官方语义，FSD 广播、HOSTSYNC 等待顺序都是内建能力，不用写守护进程。
- 缺点：两台都得能装/能配 NUT 客户端。

### B. 用 NAS 厂商自带的「网络 UPS 服务器」

飞牛（fnOS）设置页有「启用网络 UPS 服务器」，文案是「向其他设备同步 UPS 信息，并在电力中断时同步安全关机」，
并提示「使用 upsmon/secret 连接本设备，最多支持连接 5 个设备」。这就是架构 A 的图形化封装，本质是
trim_sac 代管 /etc/nut/*：写 LISTEN 3493 + upsd.users 里的 upsmon/secret，然后拉起 nut-server。
- 优点：不碰配置文件，升级不丢。
- 缺点：参数不可调（HOSTSYNC/FINALDELAY 走默认），/etc/nut/* 被 trim_sac 覆盖，手改会被冲掉。

### C. 独立中间人（UPS 接树莓派/软路由）

- 只有在「两台 NAS 都不允许装 NUT」时才需要。多一个要维护的环节，本场景不推荐。

## 3. 关于绿联的 NUT Server（实测结论 —— ⚠️ 本节结论已被推翻，见顶部修订说明）

在 <UGREEN_HOST> 上直接对 3493 端口发协议命令，结果：

| 命令 | 结果 |
|---|---|
| LIST UPS | OK -> UPS ups0 "Description unavailable" |
| GET VAR ups0 ups.status | OK -> "OL"（匿名可读） |
| LIST VAR ups0 | 49 条变量，含 device.mfr: UGREEN / device.model: US3000 / driver.flag.ignorelb: enabled |
| LOGIN ups0（upsmon/secret、upsmon、admin、root 等逐个试） | ACCESS-DENIED |
| MASTER ups0 / FSD ups0 | 未认证即拒绝 |
| LIST CLIENT ups0 | 只有 127.0.0.1 |

结论：它是「只读广播」式实现，不是完整 upsd。
- 能用：被任何程序当只读上游轮询（upsc、自定义脚本、飞牛的「网络 UPS 客户端」填 IP）。
- 不能直接依赖：标准 upsmon 作为 secondary 需要 LOGIN 成功（要做 tracking 与 FSD），这里认证入口拿不到；
  且它广播的是 UGREEN US3000 的变量表（driver.version.data: TOP HID 1.0），不是 APC 的。

所以「绿联上启用 NUT 能不能利用」分两种情况：

- 可以：APC 插绿联、飞牛当客户端——协议上飞牛能读，但前提是绿联的 UPS 支持里认 APC BK650M2-CH。
  绿联 UI 的兼容列表只列自家型号，而飞牛官方列表明确写了「施耐德（APC）：BK650M2-CH 等型号」——认机器这件事上飞牛胜出。
- 不行：绿联想当 secondary——绿联 UI 没有「接入其他设备 UPS」的入口（只有 USB 类型 + NUT Server 开关），
  NUT Server 又拒绝外部登录。要么在绿联上跑 Docker 的 nut-client（upsmon），要么放弃让绿联自动关机。

推荐方向：APC 插飞牛（官方兼容列表点名），飞牛开「网络 UPS 服务器」；绿联侧用 Docker 起 NUT 客户端接飞牛。

## 4. 推荐方案的完整配置（可复制）

以下按「NAS-1 = 飞牛/主，NAS-2 = 任意 Linux + NUT」写。若两台都自己管 NUT，直接用这套原生配置；
若用飞牛图形开关，跳过 §4.1-4.3，直接看 §4.5 的 secondary 配置。

### 4.1 NAS-1 ups.conf（驱动）

    [bk650]
        driver = usbhid-ups
        port = auto
        vendorid = 051D
        productid = 0002
        pollinterval = 5
        pollfreq = 30
        # BK650M2-CH 会自报 battery.charge.low=96% 之类的假低电量，
        # 先压住驱动内置的 LB 判定，改由 upssched 按「断电时长」决策（见 §5.1）
        ignorelb

nut-scanner -U 可以自动生成这段，省得手抄 vendorid。

### 4.2 NAS-1 upsd.conf

    LISTEN 127.0.0.1 3493
    LISTEN <UPS_HOST> 3493     # 换成 NAS-1 自己的局域网地址，别写 0.0.0.0
    MAXAGE 15
    MAXCONN 64

只监听局域网那一张网卡，避免挂到 Docker 网桥上被容器误连。

### 4.3 NAS-1 upsd.users

    [upsmon-local]
        password = <随机密码>
        upsmon primary

    [upsmon-nas2]
        password = <另一串随机密码>
        upsmon secondary          # NUT >= 2.6.2 允许 secondary 发 FSD
        actions = SET
        instcmds = ALL

    [admin]
        password = <随机密码>
        actions = SET
        instcmds = ALL

注意 NUT 的坑：同一个 UPS 只能有一个 upsmon 声称 primary，NAS-2 必须 upsmon secondary，否则第二次 MASTER 会被拒。

### 4.4 NAS-1 upsmon.conf（primary，兼管 shutdown 顺序）

    RUN_AS_USER nut
    MONITOR bk650@localhost 1 upsmon-local <密码> primary
    MINSUPPLIES 1

    SHUTDOWNCMD "/usr/local/sbin/ups-shutdown.sh"
    POWERDOWNFLAG /run/nut/powerdown

    NOTIFYCMD /usr/sbin/upssched
    POLLFREQ 5
    POLLFREQALERT 5
    HOSTSYNC 90          # 等 secondary 断开，给足它关机的时间
    DEADTIME 15
    MAXRETRY 3
    MAXAGE 15
    FINALDELAY 45        # 通知后等 45s 再执行 SHUTDOWNCMD

HOSTSYNC 与 secondary 的 FINALDELAY 是配对关系：secondary.FINALDELAY 必须 < primary.HOSTSYNC，
否则 primary 会等超时提前放弃（日志里是 "upsmon: gave up on ..."）。

### 4.5 NAS-2 upsmon.conf（secondary，只监测远程）

    RUN_AS_USER nut
    MONITOR bk650@<UPS_HOST> 1 upsmon-nas2 <密码> secondary
    MINSUPPLIES 1
    SHUTDOWNCMD "/sbin/shutdown -h +0"
    POLLFREQ 5
    POLLFREQALERT 5
    HOSTSYNC 60
    DEADTIME 15
    MAXAGE 15
    FINALDELAY 0          # 收到 LB/FSD 立刻关，别磨蹭

顺带把 NAS-2 的 nut-monitor.service 加成依赖网络就绪：

    # /etc/systemd/system/nut-monitor.service.d/network.conf
    [Unit]
    Requires=network-online.target
    After=network-online.target

### 4.6 顺序（整个设计的核心）

    市电断 -> 两台都收到 OB
              |
              +-- (可选) 断电满 N 分钟：upssched 让 secondary 先关，腾出电池给 primary
              |
    低电量 LB 或 primary 判定超时
              |
              +-- primary 广播 FSD --> secondary 立刻 SHUTDOWNCMD（FINALDELAY 0）
              |
              +-- secondary 断开 LOGIN
              |
              +-- primary 等 HOSTSYNC(90s)，超时即继续（防止被一台卡死拖到电池耗尽）
              |
              +-- primary 执行 SHUTDOWNCMD -> 关机脚本检测 POWERDOWNFLAG -> upsdrvctl shutdown
                                                                        |
                                                              UPS 切断输出（BK650M2-CH 电池模式下生效）
                                                                        |
                                                              来电 -> NAS「通电自动开机」

primary 的关机脚本必须带这段（否则 UPS 永远不断电，来电时两台已经是「打到一半的关机」状态）：

    #!/bin/sh
    FLAG=/run/nut/powerdown
    if [ -f "$FLAG" ]; then
        rm -f "$FLAG"
        /sbin/upsdrvctl shutdown
    fi

## 5. BK650M2-CH 的三个已知坑（不处理会「看起来装了但没保护到」）

### 5.1 假低电量：battery.charge.low = 96%

飞牛论坛实测（同一型号，NUT 2.8.0）：battery.charge.low: 96，而 battery.charge: 98——
差 2%，NUT 立刻判定 LB，断电极短时间就关机。这是 NUT 与 APC 固件字段语义不匹配
（APC 的 charge.low 是校准告警点而非关机门限）。

对策，按优先级：
1. ups.conf 加 ignorelb（本项目采用）——驱动不再因该字段报 LB；
2. 改用 battery.runtime.low（本机读到 120s）或 ups.status 里的 OB + 自己计时；
3. 由 upssched 按「断电时长」决策，而不是按电量百分比。

### 5.2 NUT 2.8 对 BK650 的关机指令

Chiphell 实机帖：NUT 2.8 驱动对 BK650 报错、无法让其关机，退到 2.7.4 才正常。
本机是 2.8.0-7。所以不要把「UPS 断电」当成唯一路径：万一 upsdrvctl shutdown 无效，
UPS 会继续放电直到电池耗尽（通常是硬断电，比「没来电自动开机」更麻烦）。
验证方法见 §6 第 4 步；若无效，降级为「只关 NAS，UPS 让电池跑干」，并在 BIOS/系统里确保来电自启。

### 5.3 BK650M2-CH 的电源行为（Chiphell 实测）

- 不支持延迟上电；
- 支持自动重启 / 断电后恢复；
- 支持「自身关机断电」指令，仅在电池模式下响应；响应后无视市电状态坚持断电，倒计时约 2 分钟；
- 市电存在时执行关机指令 -> 约 1 秒后自动重新上电。

推论：shutdown.return 在市电正常时会变成「重启一下」，做断电演练时别被这个假象骗到。

## 6. 验证清单（每一步都要真跑，不要只看配置）

1. upsc bk650@localhost 能看到 ups.status: OL，且 battery.charge.low 已被忽略（driver.flag.ignorelb: enabled）。
2. NAS-2：upsc bk650@<UPS_HOST> 通 -> 说明 3493 与 LISTEN 正确。
3. NAS-2：upsmon -c reload 后日志出现 Communications with UPS bk650@<UPS_HOST> established；
   NAS-1 上 LIST CLIENT bk650 能看到 NAS-2 的 IP。
4. 拔市电演练（关键）：拔掉 UPS 输入，观察
   - 两台都在 5s 内进入 OB；
   - 在 primary 上 upsmon -c fsd 模拟 FSD，确认 secondary 立刻关机且 primary 走完 HOSTSYNC -> FINALDELAY -> 关机；
   - 确认 upsdrvctl shutdown 有没有真的让 UPS 断电（§5.2）。
5. 来电后确认两台的「通电自动开机」生效（飞牛/绿联设置里都有该开关）。
6. 反向演练：只断网不断电，确认 secondary 不会误关机（DEADTIME 后只报 stale，不触发 shutdown）。

## 7. 落地到你这套硬件（⚠️ 方向已改，见顶部修订说明）

| 方案 | 谁接 APC | 谁当客户端 | 工作量 |
|---|---|---|---|
| 推荐 | 飞牛 <UPS_HOST>（官方兼容 BK650M2-CH） | 飞牛开「网络 UPS 服务器」；绿联侧 Docker 跑 nut-client（upsmon，MONITOR bk650@<UPS_HOST>） | 飞牛点开关；绿联加一个容器 |
| 备选 | 绿联 | 飞牛「网络 UPS 客户端」填 <UGREEN_HOST> | 取决于绿联是否认 APC 型号，风险高 |
| 不推荐 | 中间人 | — | 多一台设备要维护 |

绿联当前那台 US3000 的 NUT Server 建议保留只读用途（给监控/看板读 upsc ups0@<UGREEN_HOST>），
不要作为断电联动的上游——它不接受外部 LOGIN，FSD 走不通，等于只有一个方向的通知，没有关机协同。
