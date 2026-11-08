<div align="center">

# 🛡️ PrintGuard_Aholo

**FDM 打印件的「生成 → 体检 → 修复 → 交付」工具链与 MCP 服务**

*让 AI 生成的 3D 模型真正能上打印机。*

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue)](pyproject.toml)
[![MCP](https://img.shields.io/badge/协议-Model_Context_Protocol-8A2BE2)](https://modelcontextprotocol.io)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

🌐 **在线 Demo 已部署在[魔搭社区创空间](https://modelscope.cn/spaces/your-space-name)**
（部署后替换为实际地址）：网页对话直接拿 3MF，MCP 客户端连接空间地址下的 `/mcp`。

</div>

## 💡 解决什么问题

AI 文生 / 图生 3D（Lux3D）产出的模型看着精美，直接送 FDM 打印机却常常翻车：

| 痛点 | PrintGuard_Aholo 的处理 |
|---|---|
| 不水密（破面/壳汤），切片失败 | 体素重构，输出水密实体 |
| 悬垂超 45°，拉丝坍塌 | 悬垂三分类（真悬空/桥接/贴床），不误报 |
| 壁厚过薄，一掰就碎 | 中轴 EDT 壁厚检测 + 闭环增厚 |
| 朝向随意，支撑爆炸 | 自动摆向（悬垂面积最小化） |
| 体素重构留 45° 台阶 | Taubin 去阶梯平滑，体积外形基本不变 |

交付**可直接切片的 3MF / STL**；体检结论只在管线内部驱动修复决策，
不对外输出诊断报告。

## 🏗️ 工作方式

```
prompt ──────────────────────┐
图片（URL / 本地 / base64）───┤
  · 只有文字 → 文生 3D        ├─ Lux3D 生成 ── 下载 GLB ── 尺寸归一化
  · 只有图片 → 图生 3D        │
  · 文字+图片 → 文图混合      │
本地模型文件 / 公网 URL ──────┘        │
                                       ▼
        交付件 ◀── 导出 3MF/STL ◀── 修复 ◀── 体检 ◀── 几何内核
```

- **尺寸必填**：AI 网格单位不可信，`height_mm` 锁定最大边后才做壁厚/悬垂判断；
- **默认自动修复**：生成后自动体检+修复再交付，显式 `repair=false` 才跳过；
- **异步任务**：长任务秒回 `job_id`，客户端轮询 `printguard_check_job`；
- **失败要响**：任何失败落 `failed` 并带原因，不返回半成品。

几何内核（水密性 / 悬垂与支撑 / 壁厚）不依赖任何在线服务，`pip install`
后离线可用，判定口径见 [docs/geometry-kernel.md](docs/geometry-kernel.md)。

## 🚀 快速开始

**你需要自己提供的**：① Lux3D API Key（[免费申请](https://labs.aholo3d.cn)，
只做体检/修复不需要）；② HTTP 部署时的端口/域名（自定）；其余全部有默认值，
详见 [docs/deploy.md](docs/deploy.md)。

### 安装

```bash
git clone https://github.com/milu348/PrintGuard_Aholo.git
cd PrintGuard_Aholo
python -m venv .venv && source .venv/bin/activate
pip install -e .          # 拉取 numpy/trimesh/scipy/scikit-image/rtree/lxml
```

### 接入 MCP

桌面客户端（Claude Desktop / Cursor 等）用 stdio——没有地址问题：

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

本地 / 服务器 HTTP 形态：

```bash
PRINTGUARD_TRANSPORT=streamable-http PRINTGUARD_PORT=9000 .venv/bin/printguard-aholo
# MCP 端点 http://127.0.0.1:9000/mcp · 健康检查 /healthz · 下载 /d/<token>
```

服务器 + 域名 + 反向代理的完整步骤（`.env` 模板、nginx 配置）见
[docs/deploy.md](docs/deploy.md)；Docker 同理（镜像自带健康检查）。

### 命令行

```bash
printguard run "一个简约的手机支架" --height-mm 100 --quality fast   # 生成+修复+交付
printguard run --image photo.jpg --height-mm 120                     # 图生 3D
printguard run --from-file model.stl --height-mm 120                 # 修复已有模型
printguard analyze model.stl --height-mm 120 --json report.json      # 只体检（本地调试）
printguard selftest                                                  # 几何内核自测
```

## 🧰 MCP 工具

| 工具 | 类型 | 说明 |
|---|---|---|
| `printguard_run` | 异步 | 一条龙：生成（文生/图生/混合）→ 体检 → 修复 → 交付；**默认自动修复** |
| `printguard_repair` | 异步 | 修复已有模型（URL 或本地路径） |
| `printguard_check_job` | 同步 | 轮询任意异步任务 |
| `lux3d_text_to_3d` | 异步 | 纯生成：文生 / 文图混合（**不修复**，仅用户明确不要修复时用） |
| `lux3d_image_to_3d` | 异步 | 纯生成：图生（单图或 1-32 张多视角，**不修复**） |
| `lux3d_get_task` / `lux3d_list_tasks` | 同步 | 平台任务直查 / 分页 |

```jsonc
// printguard_run(prompt="手机支架", height_mm=100) 立即返回：
{"job_id": "48493ea0ae25", "status": "queued", "kind": "printguard_run"}

// 轮询到终态：
{"status": "succeeded", "result": {
  "ok": true, "mode": "text_to_3d",
  "downloads": {"model": {"url": "https://.../d/<token>",
                          "filename": "printable.3mf"}},   // 限时 5 分钟
  "warnings": [], "duration_sec": 4.7 }}
```

## 📦 交付产物

- 交付物只有模型：`printable.3mf`（默认，含单位信息）/ `printable.stl`（回退）；
- 托管形态不落盘：成功后读入内存、删 run 目录，返回 `/d/<token>` 限时链接；
- 本地 CLI 产物留在 `PRINTGUARD_RUNS_DIR/<run_id>/`，用本地路径读取；
- 不生成诊断报告、不输出任何图片。

## ⚙️ 配置

全部经环境变量注入，**`.env.example` 是唯一权威模板**（每项带注释与默认值）：

```bash
cp .env.example .env    # 填 AHOLO_API_KEY 与你的地址，其余不动也能跑
```

必须自己提供的只有：**API Key**（生成功能）与 **对外地址**（HTTP 部署时）；
数据库、渲染服务等一律不需要。完整变量表见
[docs/deploy.md](docs/deploy.md#全部环境变量)。

## 🧪 测试

```bash
.venv/bin/python -m unittest discover -s tests -t .        # 全量离线单测
PRINTGUARD_SLOW_TESTS=1 .venv/bin/python -m unittest tests.test_geometry_offline
.venv/bin/python -m printguard_aholo.cli selftest          # 几何内核自测
```

## 📁 项目结构

```
printguard_aholo/            # 核心包：检查 / 修复 / MCP 服务（可独立运行）
├── server.py                # 入口：Settings → build_app → 运行
├── config.py                # Settings（环境变量只在 from_env() 读一次）
├── cli.py                   # printguard 命令行
├── core/                    # 几何内核：profiles/meshio/voxel/inspect/repair/selftest
├── lux3d/                   # 平台客户端 + Asset 上传链
├── pipeline/                # 编排：generate/repair_run/execute/inputs/delivery
└── service/                 # MCP 服务：app/tools(7工具)/web/jobs
```

依赖方向：`config → service → tools/web → server`；`core → lux3d → pipeline`。
几何内核零服务依赖，可被 CLI / MCP / 任何调用方直接复用。

更多文档：[部署指南](docs/deploy.md) · [几何内核口径](docs/geometry-kernel.md) ·
[管线与 MCP 接入](docs/pipeline.md) · [Lux3D 集成笔记](docs/lux3d-integration-notes.md)

## ⚠️ 已知局限

- 体素重构有损：细节磨平到 pitch（0.4–2.0mm）；10 万面级非水密模型修复
  需 20-30s / ~1.6GB（托管配置），加平台生成 1-3 分钟，一轮任务 3-5 分钟正常；
- 壁厚为体素量化估计，仅薄弱区做射线精测；
- 文图混合只支持单张参考图（平台限制）；多图走 img-to-3d 多视角；
- 自动摆向只枚举有限候选姿态，不保证全局最优；
- 任务状态在内存，服务重启后历史 `job_id` 失效；交付链接限时 5 分钟，过期重新生成；
- 生成任务可长达数分钟，`wait()` 上限 20 分钟。

## 📄 License

[MIT](LICENSE) © PrintGuard_Aholo contributors
