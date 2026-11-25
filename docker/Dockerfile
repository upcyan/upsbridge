# upsbridge Docker 版 —— 从仓库根构建：
#   docker build -f docker/Dockerfile -t upsbridge .
FROM python:3.11-slim

# util-linux 提供 nsenter：关机时要进宿主 PID 1 执行 shutdown（见 README）
RUN apt-get update \
    && apt-get install -y --no-install-recommends util-linux \
    && rm -rf /var/lib/apt/lists/*

COPY app/target/upsbridge/upsbridge.py /app/upsbridge.py

# 容器内没有 /sbin/shutdown（停容器 ≠ 关宿主机），
# 默认关机命令改成 nsenter 进宿主 PID 1 执行；需要 privileged + pid: host（见 compose）。
ENV UPSBRIDGE_SHUTDOWN_CMD="nsenter -t 1 -m -u -i -n -p -- /sbin/shutdown -h now"

EXPOSE 28190
VOLUME /data

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:28190/api/health', timeout=4)"

# /data 挂配置与日志（持久卷），改端口用 -e/--entrypoint 覆盖
ENTRYPOINT ["python", "/app/upsbridge.py", \
            "--config", "/data/config.json", \
            "--log", "/data/log/app.log", \
            "--port", "28190"]
