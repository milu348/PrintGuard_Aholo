<div align="center">

# 🛡️ PrintGuard_Aholo

**FDM 打印件的「生成 → 体检 → 修复 → 交付」工具链与 MCP 服务**

*让 AI 生成的 3D 模型真正能上打印机。*

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue)](pyproject.toml)
[![MCP](https://img.shields.io/badge/协议-Model_Context_Protocol-8A2BE2)](https://modelcontextprotocol.io)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Docker](https://img.shields.io/badge/部署-Docker%20%7C%20stdio%20%7C%20Streamable%20HTTP-2496ED)](#-快速开始)

</div>

## 💡 解决什么问题

AI 文生 3D（Lux3D 等）产出的模型看起来精美，直接送 FDM 打印机却常常翻车：

| 痛点 | 后果 | PrintGuard_Aholo 的处理 |
|---|---|---|
| 不水密（破面 / 壳汤） | 切片直接失败 | 体素重构，输出水密实体 |
| 悬垂超 45° | 拉丝、坍塌、报废 | 悬垂三分类（真悬空 / 桥接 / 贴床），区分真问题与误报 |
| 壁厚过薄 | 一掰就碎 | 中轴 EDT 壁厚检测 + 闭环增厚修复 |
| 朝向随意 | 支撑爆炸 | 自动摆向（悬垂面积最小化） |
| 体素重构留下 45° 台阶 | 表面像搓板 | Taubin 去阶梯平滑，体积与外形尺寸基本不变 |

修复完成后交付**可直接切片的 3MF / STL**；如果输入来自概念图 / 四视图，
交付目录里还会带上这些原图。服务端不生成诊断报告文件。

## 🏗️ 工作方式

```
prompt / image / images ──┐
                          ├─ Lux3D 生成 ── 下载 GLB ── 尺寸归一化
本地文件 / 公网 URL ──────┘                              │
                                                         ▼
        交付件 ◀── 导出 3MF/STL ◀── 修复 ◀── 体检 ◀── 几何内核
           │
           └── 交付图：concept_image / four_view（由 Lux3D 提供）
```

- **尺寸必填**：AI 网格单位不可信，`height_mm` 把最大边锁到目标毫米数后才做壁厚 / 悬垂判断。
- **生成串行**：Lux3D 生成类任务账号级并发为 1，服务端单 worker 排队执行。
- **异步任务**：生成与修复工具立即返回 `job_id`，客户端轮询，避免长任务把 MCP 调用拖超时。
- **失败要响**：参数错、生成失败、下载为空、导出失败都会落到 `failed` 并带原因，不返回半成品。

## ✨ 几何内核

体检与修复不依赖任何在线服务，`pip install` 后即可离线使用：

| 能力 | 方法 |
|---|---|
| 水密性 | 边界边 / 非流形边 / 退化面计数 |
| 悬垂与支撑 | 法线判定 + 沿重力下探 10mm，区分真悬空 / 桥接 / 贴床 |
| 壁厚 | 体素 EDT 中轴估计 + 薄弱区射线精测，`fail / warn / pass` 三级 |
| 小孔 | 背景 EDT + 连通域 + 环绕射线，识别会堵塞的孔 |
| 孤岛 | 连通体拆分，标记体积过小的碎片 |
| 表面平整度 | 相邻面二面角分布，验证去阶梯效果 |
| 风险评分 | 加权扣分，S / A / B / C / D 五档 |
| 修复 | 碎片清理 → 体素重构 → Taubin 平滑 → 薄壁增厚 → 自动摆向 |

内置 `generic-0.4`（0.4 喷嘴 / 0.2 层高 / 0.8mm 最小壁厚 / 45° 悬垂）与
`mini-0.2` 两套 profile，也支持自定义 JSON。细节见
[docs/geometry-kernel.md](docs/geometry-kernel.md)。

## 🚀 快速开始

### 环境要求

- Python **3.10+**
- 生成新模型需要 [Lux3D API Key](https://labs.aholo3d.cn)（免费申请）
- **体检 / 修复已有模型不需要任何 Key**

### 安装

```bash
git clone https://github.com/milu348/PrintGuard_Aholo.git
cd PrintGuard_Aholo

python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

# 只做几何体检/修复（核心依赖）
pip install -e .

# 需要上传本地文件到 Lux3D（Asset SDK）
pip install -e ".[upload]"
```

> 首次安装会拉取 `numpy` / `trimesh` / `scipy` / `scikit-image` / `rtree` / `lxml`，
> 体积较大但都是几何内核的硬依赖（marching cubes、射线求交、3MF 导出）。

### 方式一：本地 stdio（桌面 Agent）

在 Claude Desktop / Cursor / OpenCode 等 MCP 客户端配置中添加：

```json
{
  "mcpServers": {
    "printguard-aholo": {
      "command": "/absolute/path/to/PrintGuard_Aholo/.venv/bin/printguard-aholo",
      "env": {
        "AHOLO_API_KEY": "your-key-here"
      }
    }
  }
}
```

也可以不用控制台脚本：

```json
{
  "command": "python",
  "args": ["-m", "printguard_aholo.server"],
  "cwd": "/absolute/path/to/PrintGuard_Aholo"
}
```

`AHOLO_API_KEY` 可留空：只做修复时完全不需要；需要生成时也可在单次工具调用里传
`api_key` 参数。

### 方式二：Streamable HTTP 服务

```bash
PRINTGUARD_TRANSPORT=streamable-http \
PRINTGUARD_HOST=127.0.0.1 \
PRINTGUARD_PORT=7860 \
PRINTGUARD_RUNS_DIR=/tmp/printguard-runs \
.venv/bin/printguard-aholo
```

- MCP 端点：`http://127.0.0.1:7860/mcp`
- 健康检查：`http://127.0.0.1:7860/healthz`
- 产物下载：`http://127.0.0.1:7860/files/<run_id>/<filename>`

```bash
curl -s http://127.0.0.1:7860/healthz | python -m json.tool
```

### 方式三：Docker

```bash
docker build -t printguard-aholo .
docker run --rm -p 7860:7860 \
  -e AHOLO_API_KEY=your-key-here \
  -v "$PWD/output:/app/output" \
  printguard-aholo
```

镜像内固定使用 `streamable-http`、`0.0.0.0:7860`，并自带 `HEALTHCHECK`
（请求 `/healthz`）。密钥文件已在 `.dockerignore` 中排除，不会打进镜像。

### 部署到魔搭创空间

1. 用本仓库作为创空间代码源（Docker 形态，监听 `7860`）；
2. 配置环境变量：
   - `AHOLO_API_KEY`：生成能力所需；
   - `PRINTGUARD_RUNS_DIR`：产物目录，建议挂载持久化存储；
   - `PRINTGUARD_PUBLIC_BASE_URL`：创空间公网地址，用于生成可点击的 `artifact_urls`；
3. 平台健康检查指向 `/healthz`，MCP 客户端连接 `/mcp`。

> 未配置 `PRINTGUARD_PUBLIC_BASE_URL` 时，`artifact_urls` 返回相对路径 `/files/...`，
> 由调用方按自己连接的服务地址拼接；stdio 形态下没有 HTTP 服务，请直接使用
> `artifacts` 中的本地绝对路径。

## 🧰 MCP 工具

| 工具 | 类型 | 说明 |
|---|---|---|
| `printguard_run` | 异步 | 一条龙：生成 → 体检 → 修复 → 交付 |
| `printguard_repair` | 异步 | 修复已有模型（公网 URL 或服务端本地路径）|
| `printguard_check_job` | 同步 | 查询任意异步任务的 `job_id` |
| `lux3d_text_to_3d` | 异步 | 文生 3D（可选参考图 / 风格）|
| `lux3d_image_to_3d` | 异步 | 单图或多视角图生 3D |
| `lux3d_concept_image` | 异步 | 概念图（先锁定设计，再进 3D）|
| `lux3d_four_view` | 异步 | 四视图（正 45° / 对侧 45° / 侧 / 背）|
| `lux3d_get_task` | 同步 | 直查 Lux3D 平台任务 |
| `lux3d_list_tasks` | 同步 | 分页查询平台生成记录 |

所有异步工具都返回同样的结构：

```json
{
  "job_id": "48493ea0ae25",
  "status": "queued",
  "kind": "printguard_repair",
  "hint": "用 printguard_check_job(job_id) 轮询，建议每 5-10 秒一次"
}
```

轮询 `printguard_check_job` 到终态后：

```json
{
  "job_id": "48493ea0ae25",
  "status": "succeeded",
  "result": {
    "ok": true,
    "run_id": "repair_20261106_120000",
    "mode": "repair",
    "artifacts": {"model": "/app/output/runs/.../printable.3mf",
                  "model_format": "3mf"},
    "artifact_urls": {"model": "https://.../files/.../printable.3mf"},
    "warnings": [],
    "duration_sec": 4.7
  }
}
```

失败时 `status` 为 `failed`，`error` 形如 `PipelineError: 源文件不存在：...`，
可直接展示给用户或重试。

### 典型用法

**一步生成可打印模型**

```
printguard_run(prompt="一个能放手机的桌面支架", height_mm=100, quality="fast")
```

**修复已有模型**

```
printguard_repair(source="https://example.com/model.glb", height_mm=120, fmt="3mf")
```

**概念先行（用图片迭代，降低成本）**

```
lux3d_concept_image(prompt="...")   →  job_id
printguard_check_job(job_id)        →  result.outputs[0]（概念图 URL）
lux3d_four_view(image=<概念图>)      →  job_id → 4 个视图 URL
printguard_run(images=[...], height_mm=120)
                                    →  3D 生成 + 体检 + 修复 + 交付
                                       （四视图原样作为交付图）
```

平台签名 URL 不能直接作为下游生成任务的输入（实测会被秒拒）。把上一环节的
URL 直接传给工具即可，服务端会自动 `download → Asset 上传 → 干净 URL` 中转；
原图同时会落进 run 目录作为交付图。

## 💻 命令行

不依赖 MCP 服务，适合本地调试与批处理：

```bash
# 一条龙：生成 + 体检 + 修复 + 交付
printguard run "一个简约的手机支架" --height-mm 100 --quality fast

# 跳过生成，直接修复已有模型
printguard run --from-file model.stl --height-mm 120 --name demo

# 多视角图生 3D（本地图片需先 pip install -e ".[upload]"）
printguard run --images front.png side.png back.png --height-mm 120

# 只做体检（命令行分析输出，不进入交付目录）
printguard analyze model.stl --height-mm 120 --json analysis.json

# 只做几何修复（只导出 STL）
printguard repair model.stl -o fixed.stl --height-mm 120

# 几何内核自测（8 个合成用例，约 90 秒）
printguard selftest
```

常用开关：`--no-orient` / `--no-smooth` / `--fmt {3mf,stl}` /
`--quality {fast,fine}` / `--api-key`。

两档质量预设：

| 档位 | 生成版本 | 面数 | 修复 pitch | 适用 |
|---|---|---|---|---|
| `fast` | G1-Turbo | 100k | 自适应 | 结构件、快速迭代 |
| `fine` | G1 | 300k | 0.3mm | 手办、细节件（更慢，更保真）|

## 💬 网页 Agent 形态

同一个容器里除了 MCP 服务，还带一个聊天前端（部署后访问站点根路径即可）：

```
你的消息 ──▶ 大模型（function calling）──▶ 进程内 MCP 工具（同 9 个）
                    ▲                              │
                    └──── 工具结果回传 ◀── 生成 / 修复 / 体检 / 限时交付
```

- 用户用自然语言描述需求（可顺手填「最大边 mm」）
- 大模型决定何时出概念图 → 四视图 → 3D 生成，缺尺寸会先反问，不替用户猜
- 图片直接在对话里预览（点图放大），模型给出「⬇ 下载模型」按钮
- 下载链接限时有效（模型 5 分钟 / 图片 60 分钟，可重复下载），到期从内存清除，
  **创空间磁盘上不留生成结果**
- 回合异步化：`POST /api/chat` 秒回 `job_id`，前端轮询 `GET /api/job/<job_id>`——
  概念图→四视图→建模可能 5-8 分钟，同步等待会被网关 504

需要配两个环境变量：`PRINTGUARD_LLM_TOKEN`（魔搭大模型 token）与
`PRINTGUARD_LLM_MODEL`；MCP 客户端形态则照旧连 `/mcp`，两者互不影响。

## 📦 交付产物

每次 run 落在 `PRINTGUARD_RUNS_DIR/<run_id>/` 下：

```
output/runs/<run_id>/
├── printable.3mf          # 交付模型（3MF 导出失败自动回退 STL）
├── concept_image.png      # 概念图（单图生 3D / 概念先行，可选）
├── four_view_01.png       # 四视图（多视角图生 3D，可选，按数量编号）
└── reference_image.png    # 文生 3D 的参考图（可选）
```

交付图直接使用 Lux3D 产出的概念图 / 四视图，原样保存，**不做本地渲染**；
不生成 `report.md` / `report.json`，服务结果也不返回体检 / 修复摘要。

托管形态（创空间）下这些文件**不长期留在磁盘**：任务成功后读入内存、删除 run
目录，返回限时链接 `/d/<token>`——**模型 5 分钟、图片 60 分钟**，到期自动从内存
清除（下载次数不限，避免浏览器预览把一次性 token 提前吃掉）。本地 stdio 形态
仍是 `/files/<run_id>/<filename>`，文件留在 run 目录由你管理。

## ⚙️ 配置

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `AHOLO_API_KEY` | 空 | Lux3D API Key；也可在单次工具调用传 `api_key` |
| `PRINTGUARD_TRANSPORT` | `stdio` | `stdio` 或 `streamable-http` |
| `PRINTGUARD_HOST` | stdio 下 `127.0.0.1`，HTTP 下 `0.0.0.0` | 监听地址 |
| `PRINTGUARD_PORT` / `PORT` | `7860` | HTTP 端口 |
| `PRINTGUARD_HTTP_PATH` | `/mcp` | MCP 端点路径 |
| `PRINTGUARD_RUNS_DIR` | `output/runs` | run 目录根，容器内建议绝对路径 |
| `PRINTGUARD_PUBLIC_BASE_URL` | 空 | 公网前缀，用于生成下载链接 |
| `PRINTGUARD_DELIVERY` | `auto` | `auto`（托管自动）/ `memory`（一次性交付）/ `files`（留在磁盘） |
| `PRINTGUARD_DELIVERY_TTL_MIN` | `5` | 模型下载链接有效期（分钟），到期从内存清除 |
| `PRINTGUARD_DELIVERY_IMAGE_TTL_MIN` | `60` | 图片有效期（分钟）；预览要重复加载，给长一些 |
| `PRINTGUARD_DELIVERY_ONCE` | `0` | 严格「下载一次即失效」；默认关（浏览器预览会提前吃掉一次性 token）|
| `PRINTGUARD_DELIVERY_MAX_MB` | `256` | 一次性交付的内存总上限 |
| `PRINTGUARD_RUN_RETENTION_HOURS` | 托管 `1` / 本地 `168` | 启动时清理过期 run 目录 |
| `PRINTGUARD_LLM_TOKEN` | 空 | 前端 Agent 用的大模型 token（ModelScope API-Inference） |
| `PRINTGUARD_LLM_MODEL` | `Qwen/Qwen3.5-27B` | Agent 使用的模型（可换成账号有额度的任一模型） |
| `PRINTGUARD_LLM_JOB_TIMEOUT_SEC` | `720` | Agent 回合内单个任务的最长等待 |
| `PRINTGUARD_LLM_MAX_ATTEMPTS` | `4` | 大模型限流/5xx 的重试次数（免费额度常 429）|
| `PRINTGUARD_LLM_RETRY_BACKOFF_SEC` | `8` | 重试退避基数（秒，按次数线性增长）|
| `PRINTGUARD_LOG_LEVEL` | `INFO` | 日志级别（日志只走 stderr）|
| `PRINTGUARD_VERBOSE` | `0` | 设为 `1` 打印客户端请求细节 |
| `PRINTGUARD_MAX_VOXELS` | `1.5e8` | 体素上限，低内存环境可收紧（自动放粗 pitch）|
| `PRINTGUARD_ALLOW_PRIVATE_URLS` | `0` | 设为 `1` 允许下载内网 / 保留地址（仅本地调试）|

API Key 只从工具参数或环境变量读取。请勿把 `AholoAPI.json`、`.env` 等密钥文件
提交到仓库——本仓库的 `.gitignore` / `.dockerignore` 已默认排除。

## 🧪 测试

```bash
# 全量离线单测（不发真实网络请求）
.venv/bin/python -m unittest discover -s tests -t .

# 含慢速用例的完整几何回归（约 90 秒）
PRINTGUARD_SLOW_TESTS=1 .venv/bin/python -m unittest tests.test_geometry_offline

# 几何内核自测
.venv/bin/python -m printguard_aholo.cli selftest
```

| 测试文件 | 覆盖 |
|---|---|
| `test_geometry_offline.py` | 体检 / 修复算法与合成用例 |
| `test_lux3d_offline.py` | Lux3D 客户端契约、重试、SSRF、rehost |
| `test_pipeline_offline.py` | 管线端到端、生成输入路由、参数校验 |
| `test_server_offline.py` | MCP 工具注册、任务状态、产物下载与路径穿越防护 |
| `test_jobs_offline.py` | 单 worker 队列与状态机 |

## 📁 项目结构

```
printguard_aholo/
├── server.py      # 入口：装配 → 注册 → 运行（stdio / streamable-http）
├── app.py         # FastMCP 实例 + 单 worker 任务队列
├── config.py      # 环境变量与运行参数（含 MCP instructions）
├── tools.py       # 9 个 MCP 工具（生成层 / 管线层 / 任务查询）
├── web.py         # HTTP 路由：/healthz、/、/files/<run_id>/<filename>
├── jobs.py        # 单 worker 异步任务队列
├── pipeline.py    # 生成 → 体检 → 修复 → 交付编排
├── delivery.py    # 一次性交付：内存取件 + 一次性链接，run 目录不长期保留
├── agent.py       # 前端 Agent：大模型 function calling → MCP 工具
├── llm.py         # ModelScope API-Inference 客户端（OpenAI 兼容）
├── static/index.html  # 网页聊天前端
├── lux3d.py       # Lux3D 开放平台客户端
├── inspect.py     # 几何体检
├── repair.py      # 几何修复
├── voxel.py       # 采样体素化与实心化
├── meshio.py      # 网格加载 / 尺寸归一化
├── profiles.py    # 打印机 profile 与阈值
├── selftest.py    # 合成用例回归
└── cli.py         # printguard 命令行
```

依赖方向单向：`config → app → tools / web → server`；`profiles → voxel → inspect → repair → pipeline`。

更多文档：

- [几何内核与判定口径](docs/geometry-kernel.md)
- [一条龙管线与 MCP 接入](docs/pipeline.md)
- [Lux3D 集成笔记（真机实测）](docs/lux3d-integration-notes.md)

## ⚠️ 已知局限

- 体素重构**有损**：细节会被磨平到 pitch（默认最大边 / 160，0.4–2.0mm）；
- 壁厚是体素量化估计，仅薄弱区做射线精测；
- **3D 生成开放 API 只返回 GLB，没有模型预览图**：交付图只来自概念图 / 四视图，
  纯 prompt 生成没有交付图；需要视觉稿请先走概念先行流程；
- 自动摆向枚举有限的候选姿态，不保证全局最优；
- 任务状态保存在内存中，服务重启后历史 `job_id` 失效，客户端重试即可；
- 交付链接限时有效：模型 5 分钟、图片 60 分钟；刷新页面不丢会话，但链接过期要重新生成；
- 前端 Agent 需要 `PRINTGUARD_LLM_TOKEN`；未配置时网页会提示，MCP 客户端形态不受影响；
- 生成任务可能持续数分钟，`wait()` 默认上限 20 分钟；
- 交付目前落在服务端本地目录，公网可访问性取决于部署时的 `PRINTGUARD_PUBLIC_BASE_URL`
  与平台网关策略。

## 📄 License

[MIT](LICENSE) © PrintGuard_Aholo contributors
