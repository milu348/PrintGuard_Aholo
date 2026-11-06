# 一条龙管线（生成 → 体检 → 修复 → 交付）

`printguard_aholo/pipeline.py` 把生成层（Lux3D）、几何内核（inspect/repair）、
交付图与模型导出串成一条阻塞式管线。MCP 层只负责把它放进单 worker 队列，
客户端拿 `job_id` 轮询，不直接等长任务。

**只交付模型与图片**：不生成诊断报告（Markdown / JSON），结果也不回体检摘要。

**交付图由 Lux3D 提供**：概念图 / 四视图原样落盘，不做本地渲染。
3D 生成任务的开放 API 只返回 GLB，没有模型预览图。

```
prompt / image / images ──┐
                          ├─ Lux3D 生成 ── 下载 GLB ── 尺寸归一
本地文件 / URL ───────────┘                              │
                                                         ▼
                    交付件 ◀── 修复 ◀── 体检 ◀── analyze_mesh
```

## 三个入口

```python
from printguard_aholo.pipeline import run_repair, run_generate, run_pipeline

# 1) 修复已有模型（本地文件或公网 URL）
run_repair("model.glb", height_mm=120, fmt="3mf")

# 2) 生成 + 体检 + 修复 + 交付
run_generate("a desk organizer with two pen slots", height_mm=120,
             quality="fast", style="photorealistic")

# 3) 统一入口：给 source 走修复，否则走生成
run_pipeline("...", height_mm=120, quality="fast")
```

共同参数：

| 参数 | 说明 |
|---|---|
| `height_mm` | **必填**，最大边缩放到该毫米数（AI 网格单位不可信） |
| `name` | run 目录名，白名单 `[A-Za-z0-9._-]{1,64}`，防目录穿越 |
| `out_dir` | 默认 `output/runs` |
| `fmt` | `3mf`（默认，含单位信息）或 `stl`；3MF 导出失败自动回退 STL |
| `orient` | 自动摆向（默认开） |
| `smooth` | 体素重构后的 Taubin 去阶梯（默认开） |
| `client` | 注入 `Lux3DClient`（测试/复用连接）；不传则按 `api_key` 新建 |

生成输入路由与交付图：

| 输入 | 平台能力 | 交付图 |
|---|---|---|
| `prompt` | 文生 3D | 无（生成 API 不返回预览图） |
| `prompt` + `image` | 文生 3D + 参考图 | `reference_image.<ext>` |
| 只有 `image` | 单图生 3D | `concept_image.<ext>` |
| `images=[...]` | 多视角图生 3D（概念图/四视图结果） | `four_view_01.<ext>` … |

`images` 里的平台签名 URL（含 `sign=`）会自动 `rehost` 中转——实测签名 URL
不能直接作为下游生成任务输入。**原图会先下载保存**，再做中转。

## 质量档

| 档位 | 版本 | 面数 | 修复 pitch | 适用 |
|---|---|---|---|---|
| `fast` | G1-Turbo | 100k | 自适应 | 结构件、快速迭代 |
| `fine` | G1 | 300k | 0.3mm | 手办、细节件（慢，保真） |

`version` / `face_count` 可单独覆盖预设。

## run 目录产物

```
output/runs/<run_id>/
├── printable.3mf          交付模型（必需）
├── concept_image.png      概念图（单图生 3D / 概念先行，可选）
├── four_view_01.png       四视图（多视角图生 3D，可选，按数量编号）
└── reference_image.png    文生 3D 的参考图（可选）
```

交付图直接使用 Lux3D 产出的图片，失败只记 `warnings`，不阻塞模型交付；
模型导出失败才抛错。不生成 `report.md` / `report.json`。

## 返回结构

`run_*` 只返回交付所需的最小字段：

```json
{
  "ok": true,
  "run_id": "...",
  "mode": "text_to_3d",
  "artifacts": {"model": "...", "model_format": "3mf"},
  "warnings": [],
  "duration_sec": 42.1
}
```

体检结果仅在内部驱动修复决策，不写文件、不回传。

## 失败语义

| 情况 | 行为 |
|---|---|
| 参数非法（高度/名称/输入组合） | `ValueError`，不建 run 目录 |
| 生成任务失败/取消/超时 | `Lux3DError`（带 taskid 与平台原因） |
| 任务成功但无 GLB/ZIP | `PipelineError` |
| 源文件不存在 / URL 无 key | `PipelineError` |
| 模型导不出 | `PipelineError` |
| 交付图保存失败 | 记 `warnings`，继续交付 |

MCP 层把这些异常统一落到 job 的 `failed` + `error`，客户端可重试。

## 接入 MCP

管线有三条出口，同一份实现：

| 出口 | 入口 | 说明 |
|---|---|---|
| MCP 客户端 | `/mcp`（stdio / streamable-http） | 9 个工具原样暴露 |
| 网页 Agent | `/` + `/api/chat` + `/api/job/<job_id>` | 大模型 function calling 调同样 9 个工具；回合异步（提交→轮询），避免网关 504 |
| 命令行 | `printguard run/analyze/repair` | 本地脚本 / CI |

MCP 侧（管线层两个异步工具；生成层 4 个、查询 3 个见 README）：

| 工具 | 说明 |
|---|---|
| `printguard_run` | 生成 → 体检 → 修复 → 交付（`prompt` / `image` / `images` 三选一） |
| `printguard_repair` | 已有模型（URL / 服务端本地路径）→ 体检 → 修复 → 交付 |
| `printguard_check_job` | 生成层与管线层统一的 `job_id` 查询 |

```json
// 调用
{"height_mm": 120, "prompt": "a phone stand", "quality": "fast", "fmt": "3mf"}
// 返回
{"job_id": "…", "status": "queued", "kind": "printguard_run"}
```

轮询 `printguard_check_job` 到 `succeeded` 后，`result` 即上面的 summary，
其中 `artifacts` 是服务端本地路径，`artifact_urls` 是 HTTP 下载链接。

管线是阻塞函数，直接放进现有 `JobManager`：

```python
job_id = JOBS.submit(
    "printguard_run",
    lambda: run_pipeline(prompt, height_mm=height, quality="fast",
                         api_key=api_key),
)
```

平台生成并发 = 1，`JobManager` 单 worker 正好匹配；`Lux3DClient` 内部还有
跨实例 create 锁，双保险。

产物下载有两条路径：

- 网页 Agent 的回合跑在独立 worker（不复用 MCP 单 worker，否则回合内的工具
  提交会把自己堵死）；`/api/chat` 只入队，`/api/job/<job_id>` 查状态
- 托管形态（默认）：任务成功后交付件转入**限时存储**，返回 `GET /d/<token>`
  （模型 5 分钟、图片 60 分钟，下载次数不限，到期从内存清除），同时删除 run
  目录——模型不长期留在磁盘
- 本地形态：`GET /files/<run_id>/<filename>`，只允许 runs 目录内一层文件
  （路径穿越返回 400）

相关环境变量：

| 变量 | 默认 | 说明 |
|---|---|---|
| `PRINTGUARD_RUNS_DIR` | `output/runs` | run 目录根；容器内建议绝对路径 |
| `PRINTGUARD_PUBLIC_BASE_URL` | 空 | 公网前缀，如 `https://xxx.ms.show`；为空时链接为相对路径 |
| `PRINTGUARD_DELIVERY_TTL_MIN` | `5` | 模型链接有效期（分钟） |
| `PRINTGUARD_DELIVERY_IMAGE_TTL_MIN` | `60` | 图片链接有效期（分钟） |
| `PRINTGUARD_DELIVERY_ONCE` | `0` | 严格一次性；默认关（预览会消耗一次性 token）|

## 命令行

```bash
# 本地修复 + 全量交付
printguard run --from-file model.stl --height-mm 120 --name demo

# 生成（key 从 AHOLO_API_KEY 或 AholoAPI.json 读取）
printguard run "a phone stand" --height-mm 120 --quality fast --style cartoon

# 多视角图生 3D（概念图/四视图结果；原图会作为交付图落盘）
printguard run --images a.png b.png --height-mm 120
```

纯 prompt 模式没有交付图；想要概念图 / 四视图，先调 `lux3d_concept_image` /
`lux3d_four_view`，再把结果 URL 传给 `--image` / `--images`。

## 测试

```bash
.venv/bin/python -m unittest tests.test_pipeline_offline -v
```

端到端用合成立方体验证 3MF 交付（只含模型，无报告/摘要）；生成路由用 FakeClient
验证文生/单图/多视角分支与签名 URL 中转，零网络。
