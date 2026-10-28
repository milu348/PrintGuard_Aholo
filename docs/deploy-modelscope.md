# PrintGuard_Aholo 云端部署指南（魔搭社区 / 任意容器平台）

目标形态：用户只填 **自己的 AHOLO API Key + 需求（尺寸/风格）**，即可通过 MCP 拿到可切片的 `printable.3mf` 与旋转预览 GIF。
平台不托管任何用户密钥，不做模型生成之外的额外计算。

---

## 1. 服务形态

| 项 | 值 |
|---|---|
| 协议 | MCP over **Streamable HTTP**（路径 `/mcp`） |
| 进程 | 单个 ASGI 进程（`mcp_server.py --transport streamable-http`） |
| 端口 | **`7860`（魔搭创空间约定）**；代码默认 8000，容器内由 `PRINTGUARD_PORT=7860` 覆盖；监听 `0.0.0.0` |
| 产物 | `printable.3mf` + `turntable.gif`（诊断图仅内部，用于 agent 讲解） |
| 交付方式 | 签名限时链接：`{base}/files/{job_id}/{name}?exp=&sig=` |
| 探针 | `GET {base}/healthz` → `{"ok": true, ...}` |

同一份代码同时支持本地 stdio（桌面宿主），云端只是换传输方式。

---

## 2. 环境变量（全部配置项，无需改代码）

| 变量 | 默认 | 说明 |
|---|---|---|
| `PRINTGUARD_TRANSPORT` | `stdio` | 云端设 `streamable-http` |
| `PRINTGUARD_HOST` | `127.0.0.1` | 容器内设 `0.0.0.0` |
| `PRINTGUARD_PORT` / `PORT` | 代码默认 `8000`；**创空间镜像内为 `7860`** | 监听端口（创空间固定 7860） |
| `PRINTGUARD_PUBLIC_BASE_URL` | 自动推导 | 对外访问基址（如 `https://xxx.ms-...`），用于拼下载链接 |
| `PRINTGUARD_FILE_SECRET` | 随机 | 下载链接签名密钥；**多副本必须一致**，否则旧链接失效 |
| `PRINTGUARD_FILE_TTL` | `3600` | 下载链接有效期（秒） |
| `PRINTGUARD_JOB_DIR` | `output/jobs` | 任务产物目录（需可写、持久） |
| `PRINTGUARD_JOB_DB` | `$JOB_DIR/jobs.db` | 任务表（SQLite） |
| `PRINTGUARD_MAX_UPLOAD_MB` | `64` | 远端输入文件体积上限 |
| `PRINTGUARD_STATELESS` | HTTP 下为 `1` | 无状态 HTTP（多副本友好） |
| `PRINTGUARD_ALLOW_PRIVATE_URLS` | `0` | 是否允许拉取内网 URL（默认禁，防 SSRF） |
| `AHOLO_API_KEY` | 空 | **仅本地用**；公网部署留空，key 由用户在 `api_key` 参数传入 |

---

## 3. 部署步骤（Docker）

```bash
docker build -t printguard-mcp .
docker run --rm -p 7860:7860 \
  -e PRINTGUARD_TRANSPORT=streamable-http \
  -e PRINTGUARD_HOST=0.0.0.0 \
  -e PRINTGUARD_PORT=7860 \
  -e PRINTGUARD_PUBLIC_BASE_URL=https://<你的域名> \
  -e PRINTGUARD_FILE_SECRET=<随机长字符串> \
  -v printguard-jobs:/data/jobs \
  printguard-mcp
```

自检：

```bash
curl -s http://127.0.0.1:7860/healthz
# {"ok":true,"transport":"streamable-http",...}
```

魔搭社区（创空间）侧一般有两种接法：

- **Docker 创空间**：把仓库根目录作为构建上下文，入口用仓库的 `Dockerfile`；
  **平台把流量转到容器 7860 端口**，镜像已按此配置（`EXPOSE 7860` + `PRINTGUARD_PORT=7860`），
  一般无需再改端口。
- **模型服务/自定义运行时**：直接 `pip install .` 后 `PRINTGUARD_TRANSPORT=streamable-http python mcp_server.py`，
  平台需要允许长连接（Streamable HTTP 用 SSE 响应流），超时时间要放宽（生成任务本身是异步 job，
  但轮询与下载是短请求）。

接入客户端（MCP 客户端配置，托管服务用 URL 形式）：

```json
{
  "mcpServers": {
    "printguard": {
      "url": "https://<你的域名>/mcp"
    }
  }
}
```

---

## 4. 用户侧交互契约

1. 宿主问用户要 **目标尺寸 mm**（工具层不猜尺寸，缺参直接报错）。
2. 宿主问用户要 **AHOLO API Key**（`labs.aholo3d.cn` 免费申请），填进 `start_printguard_job` 的 `api_key`。
3. 提交任务拿 `job_id`，轮询 `check_printguard_job`；`done` 时返回交付件签名链接 + 内联诊断缩略图。
4. 风险 C/D 时，宿主需先逐项告知用户问题再交付；薄壁修不动如实说"建议重新设计"。

只有"生成"需要 key；`analyze_printability` 与"修复已有模型"不需要 key。

---

## 5. 运维要点

| 主题 | 现状 | 建议 |
|---|---|---|
| 内存 | 体素壁厚分析峰值高（80mm 大面片件 ~7GB） | 实例 ≥8GB 内存；重任务隔离到子进程（待做） |
| 并发 | 同账号生成在进程内串行；跨用户不设限 | 上线前加全局并发闸门与每 IP 速率限制 |
| 状态 | 任务表 SQLite（单卷即可） | 多副本需共享卷；跨副本产物交付建议换对象存储 |
| 存储 | 产物落在 `PRINTGUARD_JOB_DIR` | 定时清理过期任务目录（>24h） |
| 密钥 | 用户 key 只在请求内存中出现 | 不写日志、不落盘；日志脱敏 |
| 安全 | 远端输入禁内网地址、限体积/扩展名；下载链接验签验期 | 上线前补 IP 限流与异常请求审计 |

---

## 6. 本地复现云端形态

```bash
.venv/bin/python test_mcp_http.py            # MCP over HTTP + 签名下载 + 防篡改
PRINTGUARD_TRANSPORT=streamable-http PRINTGUARD_PORT=7860 \
  .venv/bin/python mcp_server.py              # 手动起服务（创空间同端口）
```

预期：`HTTP PASS (download=200 same=True bad_sig=403 tampered=403)`。
