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
    PRINTGUARD_RUNS_DIR=/app/output/runs
# 端口不写死：平台注入 PORT 时优先用 PORT；缺省 7860（魔搭约定）

WORKDIR /app

# 只拷贝运行所需；密钥文件由 .dockerignore 排除，绝不进镜像
COPY pyproject.toml README.md ./
COPY printguard_aholo/ ./printguard_aholo/

# 国内构建用镜像源；upload = Asset SDK（本地文件输入/链式中转）
RUN pip install -i https://mirrors.aliyun.com/pypi/simple/ '.[upload]'

EXPOSE 7860

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os,urllib.request;p=os.environ.get('PRINTGUARD_PORT') or os.environ.get('PORT') or '7860';urllib.request.urlopen(f'http://127.0.0.1:{p}/healthz',timeout=4)" || exit 1

CMD ["python", "-m", "printguard_aholo.server"]
