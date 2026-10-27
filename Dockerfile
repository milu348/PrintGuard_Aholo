# PrintGuard MCP Server —— 云端托管镜像（魔搭社区 / 任意容器平台）
# 构建：docker build -t printguard-mcp .
# 运行：docker run --rm -p 7860:7860 -e PRINTGUARD_TRANSPORT=streamable-http printguard-mcp
# 端口：魔搭创空间约定 7860（平台把流量转到容器 7860）
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    MPLCONFIGDIR=/tmp/.mplcache \
    PRINTGUARD_TRANSPORT=streamable-http \
    PRINTGUARD_HOST=0.0.0.0 \
    PRINTGUARD_PORT=7860 \
    PRINTGUARD_JOB_DIR=/data/jobs

# numpy/scipy/scikit-image 的运行时依赖（部分 wheel 需要 libgomp/libgl），中文字体供诊断图渲染
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgomp1 libgl1 fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY printguard.py pipeline.py lux3d_client.py mcp_server.py ./
RUN pip install --no-cache-dir .

# 任务目录（3MF/GIF 产物 + 任务表）；多副本部署请挂同一卷或改用对象存储
RUN mkdir -p /data/jobs
VOLUME ["/data/jobs"]

EXPOSE 7860

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,os,sys; \
url='http://127.0.0.1:%s/healthz' % os.environ.get('PRINTGUARD_PORT', os.environ.get('PORT','7860')); \
sys.exit(0 if urllib.request.urlopen(url, timeout=4).status == 200 else 1)"

CMD ["python", "mcp_server.py"]
