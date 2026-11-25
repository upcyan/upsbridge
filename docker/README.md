# upsbridge Docker 版

把局域网里任意 NUT Server 的断电事件，变成这台宿主机的关机动作。核心程序与 fnOS 版同一份，平台差异由环境变量 `UPSBRIDGE_SHUTDOWN_CMD` 覆盖。

## 快速开始

```bash
cd docker
docker compose up -d          # 构建 + 启动
# 浏览器打开 http://<宿主机IP>:28190 填上游配置
```

不用 compose 也可以：

```bash
docker build -f docker/Dockerfile -t upsbridge .
docker run -d --name upsbridge --restart unless-stopped \
  --pid=host --privileged \
  -p 28190:28190 \
  -v "$PWD/docker/data:/data" \
  upsbridge
```

## 为什么需要 privileged 和 pid: host

容器里没有 `/sbin/shutdown`，而且**停容器不等于关宿主机**。默认关机命令是：

```
nsenter -t 1 -m -u -i -n -p -- /sbin/shutdown -h now
```

含义是进入宿主 PID 1 的命名空间执行关机。要成立需要两个条件：

| 参数 | 作用 | 缺了会怎样 |
|---|---|---|
| `pid: host` | 容器里能看到宿主的 1 号进程 | `nsenter -t 1` 找不到进程 |
| `privileged` | 允许切换宿主 mount/uts/ipc/net 命名空间 | nsenter 权限被拒 |

**不想要 privileged 的话**，把 `shutdown_cmd` 换成任何你能接受的命令即可（界面里改，或改环境变量），例如只弹告警不关机、或调用你自己的关机脚本。此时它永远不会真的关宿主机。

## 数据与配置

| 路径 | 内容 |
|---|---|
| `docker/data/config.json` | 配置（界面保存的都在这） |
| `docker/data/log/app.log` | 运行日志 |

重建容器不丢；换机器把 `data/` 整个拷走即可。

## 端口 / 健康检查

- 默认 `28190:28190`；改端口时 compose 里 ports 与 `--port`（ENTRYPOINT）要一起改。
- 健康检查打 `/api/health`，`docker ps` 里 healthy 才算正常。

## 首次配置

1. 打开界面，填上游 NUT Server 地址、UPS 名、账号密码，点「测试连接」；
2. **保持演练模式**，做一次真实断电演练，对着 `/data/log/app.log` 确认判定；
3. 确认无误后再关演练模式。

## 关机规则（与 fnOS 版一致）

断电持续 `shutdown_on_battery_seconds`（默认 300s）关机；`LB` 须过三道闸（电池供电 + 数值确实低 + 持续 `lb_grace_seconds`）；`FSD` 须在本次运行见过断电才认；通信中断只报警不关机。详见仓库根 README。
