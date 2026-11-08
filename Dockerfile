# PrintGuard_Aholo —— 魔搭创空间 Docker 形态（MCP 服务）
#
# 平台约定：监听 0.0.0.0:7860；MCP 端点 /mcp；健康检查 /healthz
# 本地自测：docker build -t printguard-mcp . && docker run -p 7860:7860 printguard-mcp
# 自检：    curl -s http://127.0.0.1:7860/healthz

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PRINTGUARD_TRANSPORT=streamable-http \
    PRINTGUARD_HOST=0.0.0.0 \
    PRINTGUARD_RUNS_DIR=/app/output/runs \
    PRINTGUARD_MAX_VOXELS=20000000
# 端口不写死：平台注入 PORT 时优先用 PORT；缺省 7860（魔搭约定）
# PRINTGUARD_MAX_VOXELS=2e7：托管体素预算。distance_transform_edt 内部要同时持有
# 多份同尺寸数组，内存 ≈ 体素数 × 60B（实测 28M 体素 → +1.37GB）。
# 本容器约 2GB：预算 2e7 时实测 150mm 件峰值 872MB（pitch 0.66mm），
# 3.5e7 会到 1.73GB、默认 1.5e8 直接 OOM。有更多内存可调大换取更细 pitch。

WORKDIR /app

# 只拷贝运行所需；密钥文件由 .dockerignore 排除，绝不进镜像
COPY pyproject.toml README.md ./
COPY printguard_aholo/ ./printguard_aholo/
COPY modelscope_agent/ ./modelscope_agent/

RUN pip install -i https://mirrors.aliyun.com/pypi/simple/ .

EXPOSE 7860

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os,urllib.request;p=os.environ.get('PRINTGUARD_PORT') or os.environ.get('PORT') or '7860';urllib.request.urlopen(f'http://127.0.0.1:{p}/healthz',timeout=4)" || exit 1

CMD ["python", "-m", "printguard_aholo.server"]
