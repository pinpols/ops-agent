# ops-agent T1 触发服务镜像:非 root、健康检查、只读诊断 webhook。
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    OPS_PROFILE=prod \
    OPS_METRICS_FILE=/var/run/ops-agent/metrics.prom

WORKDIR /app

# 先装依赖(利用层缓存),再拷源码;从 hash lock 派生 constraints,锁住生产镜像传递依赖版本。
COPY pyproject.toml README.md requirements.txt requirements.lock ./
COPY ops_agent ./ops_agent
COPY evals ./evals
RUN awk '/^[A-Za-z0-9_.-]+==/ { sub(/[[:space:]]+\\$/, ""); print }' requirements.lock > /tmp/constraints.txt \
    && pip install --no-cache-dir -c /tmp/constraints.txt -e . \
    && mkdir -p /var/run/ops-agent /app/.ops-agent

# 非 root 运行(最小权限);只读根文件系统时 .ops-agent / metrics 目录需可写挂载
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin opsagent \
    && chown -R opsagent:opsagent /var/run/ops-agent /app/.ops-agent
USER opsagent

EXPOSE 8080

# 健康探针:容器编排据此判存活/就绪
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz',timeout=3).status==200 else 1)"

ENTRYPOINT ["ops-agent"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8080"]
