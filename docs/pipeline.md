# 一条龙管线（生成 → 体检 → 修复 → 交付）

`printguard_aholo/pipeline/` 把生成层（Lux3D）、几何内核（inspect/repair）、
模型导出与交付串成一条阻塞式管线。服务层只负责把它放进单 worker 队列，
客户端拿 `job_id` 轮询，不直接等长任务。

**生成三种模式**（对应官方 API，互不混用）：

```
prompt ──────────────▶ text-to-3d ────────────── mode=text_to_3d
prompt + 单张参考图 ──▶ text-to-3d(img=…) ────── mode=mixed_to_3d  （文图混合）
单图 / 1-32 张图 ─────▶ img-to-3d(img/imgs=…) ── mode=image_to_3d
本地模型文件 / URL ───▶（跳过生成）体检 + 修复 ── mode=repair
```

## 模块地图

| 模块 | 职责 |
|---|---|
| `execute.py` | 执行机械：PipelineError / 参数校验 / run 目录 / 修复编排 / 导出 |
| `inputs.py` | 输入归一化：source（本地/URL）、image（URL/本地/base64→Asset URL） |
| `generate.py` | 生成入口（三种模式路由 + quality 预设） |
| `repair_run.py` | 修复入口 |
| `delivery.py` | 限时交付：内存取件 + 限时链接，run 目录不长期保留 |

## 对外入口

```python
from printguard_aholo.pipeline import run_generate, run_repair

# 1) 生成 + 体检 + 修复 + 交付（三种生成模式同一入口）
run_generate("a desk organizer with two pen slots", height_mm=120,
             quality="fast", style="photorealistic")
run_generate(height_mm=120, image="https://cdn.example/ref.jpg")          # 图生
run_generate("改成圆角", height_mm=120, image="https://cdn.example/ref.jpg")  # 混合
run_generate(height_mm=120, images=["front.png", "side.png"])             # 多视角

# 2) 修复已有模型（本地文件或公网 URL）
run_repair("model.glb", height_mm=120, fmt="3mf")
```

共同参数：

| 参数 | 说明 |
|---|---|
| `height_mm` | **必填**，最大边缩放到该毫米数（AI 网格单位不可信） |
| `name` | run 目录名，白名单 `[A-Za-z0-9._-]{1,64}`，防目录穿越 |
| `out_dir` | 默认 `output/runs` |
| `fmt` | `3mf`（默认，含单位信息）或 `stl`；3MF 导出失败自动回退 STL |
| `orient` / `smooth` | 自动摆向 / Taubin 去阶梯（默认都开） |
| `repair` | **默认 True（产品契约：生成后自动体检+修复）**；仅用户明确不要修复时传 False——跳过整条修复链，只做格式导出，warnings 带未修复提示 |
| `client` | 注入 `Lux3DClient`（测试/复用连接）；不传则按 `api_key` 新建 |
| `verbose` | 透传客户端调试日志（服务层从 Settings 传入） |

图片输入（`image` / `images`）接受三种形式：

| 形式 | 走法 |
|---|---|
| 公网 URL | 直接透传（普通公网 URL 平台可用） |
| data URI（base64） | 解码 → Asset 上传 → 干净 URL |
| 服务端本地路径 | 读文件 → Asset 上传 → 干净 URL |

> 平台签名 URL 不能直接喂给生成任务（实测秒败 status=4，见
> lux3d-integration-notes.md）；Asset URL 实测可用，这就是 Asset 中转存在的原因。

## 质量档

| 档位 | 版本 | 面数 | 修复 pitch | 适用 |
|---|---|---|---|---|
| `fast` | G1-Turbo | 100k | 自适应 | 结构件、快速迭代 |
| `fine` | G1 | 300k | 0.3mm | 手办、细节件（慢，保真） |

`version` / `face_count` 可单独覆盖预设。

## run 目录产物

```
output/runs/<run_id>/
└── printable.3mf          交付模型（必需）
```

模型导出失败才抛错；不生成 `report.md` / `report.json`，也不出任何图片。
体检结果仅在内部驱动修复决策（`repair(analyze=False)`），不写文件、不对外回传。

## 返回结构

`run_generate` / `run_repair` 只返回交付所需的最小字段：

```json
{
  "ok": true,
  "run_id": "run_20261108_101500",
  "mode": "text_to_3d",
  "artifacts": {"model": "...", "model_format": "3mf"},
  "warnings": [],
  "duration_sec": 42.1
}
```

## 失败语义

| 情况 | 行为 |
|---|---|
| 参数非法（高度/名称/输入组合） | `ValueError`，不建 run 目录 |
| 生成任务失败/取消/超时 | `Lux3DError`（带 taskid 与平台原因） |
| 任务成功但无 GLB/ZIP | `PipelineError` |
| 源文件不存在 / URL 无 key / 图片非法 | `PipelineError` / `ValueError` |
| 模型导不出 | `PipelineError` |

服务层把这些异常统一落到 job 的 `failed` + `error`，客户端可重试。

## 接入 MCP

管线有三个出口，同一份实现：

| 出口 | 入口 | 说明 |
|---|---|---|
| MCP 客户端 | `/mcp`（stdio / streamable-http） | 7 个工具原样暴露 |
| 网页 Agent | `/` + `/api/chat` + `/api/job/<job_id>` | 大模型 function calling 调同一批工具；回合异步（提交→轮询），避免网关 504 |
| 命令行 | `printguard run/analyze/repair` | 本地脚本 / CI |

MCP 侧（见 `service/tools.py`，注册表即 Agent 派发唯一来源）：

| 工具 | 说明 |
|---|---|
| `printguard_run` | 生成（prompt / image / images）→ 体检 → 修复 → 交付 |
| `printguard_repair` | 已有模型（URL / 服务端本地路径）→ 体检 → 修复 → 交付 |
| `printguard_check_job` | 生成层与管线层统一的 `job_id` 查询 |
| `lux3d_text_to_3d` / `lux3d_image_to_3d` | 纯生成（不修复不交付，回平台任务结果） |
| `lux3d_get_task` / `lux3d_list_tasks` | 平台任务直查 / 分页 |

```json
// 调用
{"height_mm": 120, "prompt": "a phone stand", "quality": "fast", "fmt": "3mf"}
// 返回
{"job_id": "…", "status": "queued", "kind": "printguard_run"}
```

轮询 `printguard_check_job` 到 `succeeded` 后，`result` 即上面的 summary，
其中 `downloads` 是限时下载链接（模型默认 5 分钟），`artifacts` 只剩格式信息
（磁盘文件已删）。

管线是阻塞函数，直接放进现有 `JobManager`：

```python
job_id = ctx.mcp_jobs.submit(
    "printguard_run",
    lambda: run_generate(prompt, height_mm=height, quality="fast",
                         client=client))
```

平台生成并发 = 1，`JobManager` 单 worker 正好匹配；`Lux3DClient` 内部还有
跨实例 create 锁，双保险。

产物下载只有一条路径：

- 网页 Agent 的回合跑在独立 worker（不复用 MCP 单 worker，否则回合内的工具
  提交会把自己堵死）；`/api/chat` 只入队，`/api/job/<job_id>` 查状态
- 任务成功后交付件转入**限时存储**，返回 `GET /d/<token>`（模型默认 5 分钟、
  下载次数不限、到期从内存清除），同时删除 run 目录——模型不长期落在磁盘
- 本地 CLI 形态不走 HTTP：产物直接落在 run 目录，用绝对路径读取

相关配置（Settings 字段，环境变量同名）：

| 字段 | 默认 | 说明 |
|---|---|---|
| `runs_dir`（PRINTGUARD_RUNS_DIR） | `output/runs` | run 目录根；容器内建议绝对路径 |
| `public_base_url`（PRINTGUARD_PUBLIC_BASE_URL） | 空 | 公网前缀，如 `https://xxx.ms.show`；为空时链接为相对路径 |
| `delivery_ttl_min`（PRINTGUARD_DELIVERY_TTL_MIN） | `5` | 模型链接有效期（分钟） |
| `delivery_once`（PRINTGUARD_DELIVERY_ONCE） | `0` | 严格「下载一次即失效」；默认关（按时效清除）|

## 命令行

```bash
# 本地修复 + 全量交付
printguard run --from-file model.stl --height-mm 120 --name demo

# 生成（key 从 AHOLO_API_KEY 或 AholoAPI.json 读取）
printguard run "a phone stand" --height-mm 120 --quality fast --style cartoon
printguard run --image photo.jpg --height-mm 120          # 图生 3D
printguard run "改成圆角" --image photo.jpg --height-mm 120  # 文图混合
```

## 测试

```bash
.venv/bin/python -m unittest tests.test_pipeline_offline -v
```

端到端用合成立方体验证 3MF 交付（只含模型，无报告/摘要）；生成路由用 FakeClient
验证文生/图生/混合/多视角分支与 Asset 中转，零网络。
