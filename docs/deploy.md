# 部署指南

PrintGuard_Aholo 的三种部署形态与全部配置项。**所有配置都有默认值**：
必须由你提供的只有 Lux3D API Key（要用生成功能时），以及由部署形态决定的
对外地址。

## 你需要自己提供

| 项 | 是否必需 | 说明 |
|---|---|---|
| **Lux3D API Key** | 生成功能必需 | 到 [labs.aholo3d.cn](https://labs.aholo3d.cn) 免费申请，填入 `AHOLO_API_KEY`；**只做体检/修复则完全不需要** |
| **对外地址** | HTTP 部署时自行决定 | 监听端口、域名、是否套反向代理（见下文） |
| **持久化目录** | 建议 | `PRINTGUARD_RUNS_DIR` 指向你自己的目录（CLI 本地用默认值即可） |

**不需要提供**：数据库（任务状态在内存）、渲染/切片服务、体检与修复零外部依赖。

## 配置怎么给

全部配置经环境变量注入，**`.env.example` 是唯一权威模板**（每一项都有注释
说明默认值与何时需要改）：

```bash
cp .env.example .env
vi .env        # 至少填 AHOLO_API_KEY（要用生成功能时）
```

两种注入方式（应用本身不自动读 `.env` 文件，避免引入额外依赖）：

```bash
# 本地进程
set -a; source .env; set +a
.venv/bin/printguard-aholo

# Docker（--env-file 原生支持）
docker run --env-file .env ...
```

## 方式一：本地 stdio（桌面 MCP 客户端）

没有地址问题：客户端直接拉起进程，stdout 即协议通道。

```json
{
  "mcpServers": {
    "printguard-aholo": {
      "command": "/absolute/path/to/PrintGuard_Aholo/.venv/bin/printguard-aholo",
      "env": { "AHOLO_API_KEY": "your-key-here" }
    }
  }
}
```

`AHOLO_API_KEY` 可留空：只做修复时完全不需要；需要生成时也可在单次工具
调用里传 `api_key` 参数。

## 方式二：HTTP 服务

```bash
PRINTGUARD_TRANSPORT=streamable-http \
PRINTGUARD_HOST=127.0.0.1 \
PRINTGUARD_PORT=9000 \
PRINTGUARD_RUNS_DIR=/tmp/printguard-runs \
.venv/bin/printguard-aholo
```

- MCP 端点：`http://127.0.0.1:9000/mcp`
- 健康检查：`/healthz`
- 产物下载：`/d/<token>`（限时链接，模型默认 5 分钟）

## 方式三：Docker

```bash
docker build -t printguard-aholo .

# 简单形态：单变量注入
docker run --rm -p 7860:7860 \
  -e AHOLO_API_KEY=your-key-here \
  -v "$PWD/output:/app/output" \
  printguard-aholo

# 完整形态：环境变量文件
docker run -d -p 7860:7860 --env-file .env \
  -v "$PWD/output:/app/output" --name printguard printguard-aholo
```

镜像内固定 `streamable-http`、`0.0.0.0:7860`，自带 `HEALTHCHECK`
（请求 `/healthz`）。密钥文件已被 `.dockerignore` 排除，不会进镜像。

## 方式四：自己的服务器 + 域名（推荐生产形态）

任意 Linux 服务器 / VPS（托管配置约 2GB 内存）：

```bash
cp .env.example .env && vi .env     # 填 AHOLO_API_KEY 与你的地址
docker build -t printguard-aholo .
docker run -d -p 7860:7860 --env-file .env \
  -v "$PWD/output:/app/output" --name printguard printguard-aholo
curl -s http://127.0.0.1:7860/healthz | python -m json.tool
```

nginx 反向代理到 `127.0.0.1:7860`，并在 `.env` 里设
`PRINTGUARD_PUBLIC_BASE_URL=https://你的域名`——交付结果里的下载链接即为
完整可点地址：

```nginx
server {
    server_name printguard.example.com;      # 换成你的域名
    location / {
        proxy_pass http://127.0.0.1:7860;
        proxy_http_version 1.1;
        proxy_buffering off;                 # streamable-http 用 SSE 流回包，必须关缓冲
        proxy_read_timeout 300s;
        proxy_set_header Host $host;
    }
}
```

MCP 客户端连接 `https://你的域名/mcp`（路径可用 `PRINTGUARD_HTTP_PATH` 修改）。

## 全部环境变量

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `AHOLO_API_KEY` | 空 | Lux3D API Key；也可在单次工具调用传 `api_key` |
| `PRINTGUARD_TRANSPORT` | `stdio` | `stdio` 或 `streamable-http` |
| `PRINTGUARD_HOST` | stdio 下 `127.0.0.1`，HTTP 下 `0.0.0.0` | 监听地址 |
| `PRINTGUARD_PORT` / `PORT` | `7860` | HTTP 端口（云平台注入 `PORT` 时优先用） |
| `PRINTGUARD_HTTP_PATH` | `/mcp` | MCP 端点路径 |
| `PRINTGUARD_RUNS_DIR` | `output/runs` | run 目录根，容器内建议绝对路径 |
| `PRINTGUARD_PUBLIC_BASE_URL` | 空 | 公网前缀，用于生成完整下载链接 |
| `PRINTGUARD_DELIVERY_TTL_MIN` | `5` | 模型下载链接有效期（分钟） |
| `PRINTGUARD_DELIVERY_ONCE` | `0` | 严格「下载一次即失效」；默认关（浏览器预取会提前吃掉 token） |
| `PRINTGUARD_DELIVERY_MAX_MB` | `256` | 限时交付的内存总上限 |
| `PRINTGUARD_RUN_RETENTION_HOURS` | `168`（7 天） | 启动时清理过期 run 目录 |
| `PRINTGUARD_LOG_LEVEL` | `INFO` | 日志级别（日志只走 stderr） |
| `PRINTGUARD_VERBOSE` | `0` | 设为 `1` 打印客户端请求细节 |
| `PRINTGUARD_MAX_VOXELS` | `1.5e8`（镜像内 `2e7`） | 体素上限，低内存环境收紧（自动放粗 pitch）。实测内存≈体素数×60B；预算优先，任何尺寸都会放粗到不超限 |
| `PRINTGUARD_ALLOW_PRIVATE_URLS` | `0` | 设为 `1` 允许下载内网 / 保留地址（仅本地调试） |

API Key 只从工具参数或环境变量读取。请勿把 `AholoAPI.json`、`.env` 等密钥文件
提交到仓库——`.gitignore` / `.dockerignore` 已默认排除。
