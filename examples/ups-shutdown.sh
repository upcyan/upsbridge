#!/bin/sh
# /usr/local/sbin/ups-shutdown.sh  —— primary 的 SHUTDOWNCMD
# 目标：先关重负载，最后让 UPS 切断输出，来电后靠「通电自动开机」恢复
set -e

FLAG="${POWERDOWNFLAG:-/run/nut/powerdown}"

# 1) 可选：先停掉重负载（Docker 容器 / 虚拟机），缩短关机时间
if [ -x /usr/trim/bin/docker-shutdown-containers.sh ]; then
    /usr/trim/bin/docker-shutdown-containers.sh || true
fi

# 2) 让 UPS 断电（BK650M2-CH 仅在电池模式下响应，见 README 5.2 / 5.3）
if [ -f "$FLAG" ]; then
    rm -f "$FLAG"
    /sbin/upsdrvctl shutdown || logger -t ups-shutdown "upsdrvctl shutdown FAILED"
fi

# 3) 关机
exec /sbin/shutdown -h +0
