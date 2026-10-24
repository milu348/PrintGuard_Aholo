# PrintGuard

面向 FDM 打印件的"生成 → 可制造性校核 → 修复"工具链。

## 概述

- Lux3D 生成 → FDM 工艺约束检测/自动修复 → 交付可直接切片的 3MF + 诊断报告图 + 旋转预览 GIF；切片与打印由用户自理。
- 形态：**pip/uvx 可安装的 MCP 能力包**（宿主无关，Claude Desktop / Cursor / DSH 皆可挂载），本地开发走 CLI + skill 指令。无前端，交互全在对话内。
- 基准参数：FDM 创客场景，0.4mm 喷嘴 / 0.2mm 层高。

## 技术决策

| 决策 | 结论 |
|---|---|
| Lux3D 接入 | 自研 Python 客户端（生成/上传/导出/拆件全链路），优先基于官方 SDK；不用官方 SkillHub skill（仅覆盖生成环节） |
| 修复粒度 | 自动修复 + 报告带科普解释，不逐项阻塞用户 |
| 尺寸策略 | `aiPredictSize=false`；本地归一化后按用户指定 mm 确定性缩放；未给尺寸时先询问 |
| 模型版本 | 测试迭代用 G1-Turbo（快、可白模）；正式资产用 G1（质量优先） |
| Python 环境 | 工作区内建 `.venv/`，不污染全局；中文路径下 venv 的 ensurepip 会失败，用 `--without-pip` + 全局 `pip --python` 安装 |
| 密钥 | `secrets.local.json`（已 gitignore），运行时注入 `AHOLO_API_KEY` 环境变量 |
| MCP 形态 | 仅 stdio（桌面宿主：Claude Desktop/Cursor/DSH）；产物绝对路径 + 诊断缩略图内联，不做托管/COS URL（v0.4） |

## API 要点（官方 OpenAPI v2.0，网关 https://api.aholo3d.cn）

鉴权：Header `Authorization: <ApiKey>`（无 Bearer 前缀）。全部任务异步：create → 轮询 `GET /lux3d/v1/generate/task/get?taskid=` → 状态 0/1 进行中、3 成功、4 失败、6 取消。结果 URL **有效期 2 小时，必须立刻下载**。**并发限制：同一账号同时仅 1 个生成任务**（GENERATION_CONCURRENCY_LIMIT_EXCEEDED），流水线必须串行。

| 能力 | 端点 | 关键参数 |
|---|---|---|
| 文生 3D | POST `/lux3d/v1/generate/text-to-3d/task/create` | prompt 必填；style 7 种；version G1/G1-Turbo；faceCount 10000–300000；outputFormat zip/glb/ply；enablePbr；aiPredictSize |
| 图生 3D | POST `/lux3d/v1/generate/img-to-3d/task/create` | img 单图或 imgs 1–32 张 |
| 概念图 | POST `/lux3d/v1/generate/multimodal-to-image/task/create` | img/prompt 至少一个 |
| 多格式导出 | POST `/lux3d/v1/multi-format-export/task/create` | modelUrl（Lux3D 产物 ZIP 或 GLB）；outputFormat：usdz/obj_zip/fbx_zip/**stl**/**3mf**，glb 输入时必填 |
| 部件拆分 | POST `/lux3d/v1/part-split/task/create` | glbUrl，返回含拆件的 GLB |
| 上传 | GET `/asset/v1/token` + OUS 分块 | 本地 mesh 回传，走导出/拆件前必须 |
| Python SDK | `pip install manycore-aholo-sdk-lux3d manycore-aholo-sdk-asset` | 已封装轮询/重试/上传 |

设计含义：
- `faceCount` 默认取 ~100000（网格更干净、利于修复），可配置；
- `enablePbr=false` 出白模，减小处理体积（仅 G1-Turbo 生效）；
- **检测必须在缩放之后进行**：所有 mm 阈值的前提是真实尺寸；
- **交付兜底**：本地 `printable.3mf` 恒为打印基准（v0.4 起官方导出链已移除——服务端存在 ×0.808 尺寸偏差）。

冒烟测试实证（G1-Turbo，任务 4317409/4317432）：
- **原始输出为归一化网格**（bbox 最大边 = 1.0，非 mm）→ `--height-mm` 本地缩放是必选项；贴床豁免层（0.3mm）等阈值也只有在缩放后才有意义；
- **真实生成 mesh 是"壳汤"**：boundary_edges=0 但 nonmanifold_edges 达数百（自交/重叠壳，非破洞）→ 体素重构是必需修复路径，manifold3d 布尔清理仅作优化项；
- G1-Turbo 生成耗时 62–77s，现场演示可接受；`faceCount=100000` 输出单 GLB ~1.8MB。

## 架构

```
用户（自然语言） → Agent 宿主（MCP 工具 / skill 指令编排）
  ├─ start_printguard_job(prompt, height_mm, quality, style, image?, file_or_url?)
  │      → Lux3D 生成（串行轮询）→ 本地检测/修复 → 交付件
  ├─ check_printguard_job(job_id)                 → 状态 + 产物绝对路径 + 内联诊断缩略图
  ├─ analyze_printability(file_or_url, height_mm) → 秒级诊断 JSON（免 key）
  └─ 交付件：printable.3mf + turntable.gif（渲染预览）(+ diagnostic_sheet.png 内部诊断件 / report.json)
```

技术栈：Python 3.13 + numpy/scipy + trimesh（体素/网格，manifold3d·skimage 后端）+ manycore SDK。

**流水线固定顺序（有序 DAG，顺序错了互相破坏）**：水密修复 → 尺寸缩放 → 壁厚/孔腔修复 → 摆放优化。每步输出 before/after 量化指标（"最小壁厚 0.3→0.9mm"、"支撑体积省 62%"）写入报告。

**渲染方案**：`diagnostic_sheet.png` = matplotlib 缺陷面着色（灰=正常/红=悬垂/蓝=贴床/菱形=最薄点 + 中文指标面板，无 GL 依赖）；`turntable.gif` = 16 帧 Lambert 头灯着色旋转预览。不引入任何需要 GPU/显示上下文的依赖；交互式 HTML 仍可由 `printguard.py report` 生成（未入交付链）。

**统一切片分析器**：小孔(#5)、桥接(#8)、孤岛(#9)共用"按层高平面截切看截面"一个引擎，一次计算服务三项检测。

## 功能清单：FDM 约束 → 检测项 → 修复策略

| # | 工艺约束 | 检测方法 | 自动修复策略 | 优先级 | 状态 |
|---|---|---|---|---|---|
| 1 | 必须水密 | 非流形边、孔洞、自交、退化面 | manifold3d 体素重构/填洞 | P0 | ✅ |
| 2 | 最小壁厚 ≥0.8mm | 体素 EDT 中轴法（T=2D-p，fail/warn/pass 三档） | 闭环体素增厚（v2，修不动如实报） | P0 | ✅ |
| 3 | 悬垂 ≤45° | 面法线 vs 重力方向 | 自动搜索最优摆放角（17 候选） | P0 | ✅ |
| 4 | 最小间隙 ≥0.4mm | 距离场 | 间隙撑开或合并 | P1 | —（v0.5 评估暂缓） |
| 5 | 小孔收缩（<3mm） | 截面圆拟合 | 补偿扩孔 +5~8% | P1 | —（v0.5 评估暂缓） |
| 6 | 密闭内腔 | 壳体包含检测 | 自动开排气孔 | P1 | —（v0.5 评估暂缓） |
| 7 | 底面能贴床 | 底面接触面积/平面度 | 摆放目标函数含床面接触项 | P1 | ✅（随 #3） |
| 8 | 桥接 ≤10mm | 截面分析 | 摆放调整 / 底部填实 | P2 | — |
| 9 | 无孤岛 | 逐层截面连通性 | 摆放调整 | P2 | — |
| 10 | 细柱/针尖易断 | 直径<2mm 细长比 | 截断+倒圆角 | P2 | 部分（壁厚检测抓发丝级并如实报） |
| 11 | 过大/异形件难打印 | 包围盒 vs 打印机行程 | Lux3D part-split 拆件，输出多文件 | P2 | ✗ 已移除（v0.5：单连体输入返回原样，对 AI 生成件无效） |

P0 = 生成模型翻车重灾区，先做；P1 = 实现简单且画面感强；P2 = 时间富余再做。

## 当前进度

### 状态速览（v0.3.1）

| 维度 | 现状 |
|---|---|
| 交付物 | `printable.3mf` + `turntable.gif`（渲染预览）；`diagnostic_sheet.png`（缺陷着色+中文指标）转内部诊断件（v0.4.1，agent 内联讲解用，不作交付承诺） |
| 已验证 | 5+ 条端到端真实生成流水线（fast/fine/图文输入，D→S/A 全部水密）、selftest 6/6、MCP stdio 协议回归、uvx 可安装包 |
| 群核服务 | 文生/图生 3D ✓、Asset 上传 ✓（参考图上传用）；概念图已实测未接入（`lux3d_client.concept_image` 可直接调用）；拆件/材质迁移已移除（v0.5 评估）；官方多格式导出链已移除（v0.4，尺寸偏差） |
| 检测项 | 水密性 / 悬垂 45° / 壁厚 EDT 三档 verdict / 风险评分 S-D ✓；小孔/间隙/内腔 v0.5 评估后暂缓（结论见日志），桥接/孤岛未做 |

以下为按时间的开发日志（决策与踩坑的完整记录，续作时先读这里）：

**v0.1.1 已跑通**（`printguard.py` + `lux3d_client.py`）：

- 本地闭环：analyze / repair / selftest；检测（水密性、悬垂 45°、风险 S–D）、修复（拓扑填洞 → 体素自适应 closing 实心化 → marching cubes → 自动摆向）；
- selftest 4 项全过：破面球 D→A（正确实心化 4487mm³）、相交双球 D→A、T 悬垂件 B→S（悬垂 0.23→0）、GLB 往返正确；
- **API 冒烟测试 PASS**：鉴权 ✓、G1-Turbo 62–77s ✓、原始尺度=归一化 ✓、真实 mesh=自交壳汤 ✓；
- **真实资产修复闭环 PASS**：手机支架（80mm）D→S、卡通小猫（60mm）D→S，产物在 `output/smoke/`；
- **一条龙 PASS**（`pipeline.py "prompt" --height-mm 100`）：桌面耳机架端到端 ~2min（47s 生成 + ~69s 本地，含壁厚分析），D→S，水密，悬垂 10.8%→1.5%，交付 `printable.stl` + `report.json` + `preview.png`；
- **v0.2 壁厚检测 PASS**：体素 EDT 中轴法（T=2D-p，round 量化补偿，pitch≤0.2）；selftest 扩到 6 项全过（0.5mm 悬空薄板 fail / 2mm 悬挑板 2.04 / 15mm 立方 15.0 / 球 20.2）；真实手机支架（80mm）min_wall 1.8mm pass、评分 98；非水密输入自动跳过壁厚（壳汤无意义）；踩坑记录：测试用例曾把"薄板叠实心底座"误当薄壁（材料连续非薄壁），及 trimesh 体素 round 量化使薄特征高估约 1 个 pitch；
- **v0.2 诊断渲染 PASS**（matplotlib Agg + fast-simplification 减面）：灰=正常/蓝=贴床/红=悬垂/红菱形=最薄点，标题带 risk+wall+overhang；CLI `render` 子命令 + pipeline 自动出 preview.png；工作区已整顿：`output/assets/`（真实生成 GLB 资产池）、`output/demo/`、`output/runs/`；`smoke_lux3d.py` 更名 `lux3d_client.py`（正式客户端模块）；
- 已知局限（诚实记录）：悬垂为几何法，堆叠贴合面会误报；体素重构有损（pitch 量级）；曲面壁厚 warn 偏保守（量化 ±2*pitch）；交付件超 120 万面自动体素重闭合降密（quadric/manifold 减面均会留非流形边，实测弃用）+ 3MF 压缩交付（九尾狐 62MB→11.2MB）。
- 工作区清理（本轮）：删除 selftest 再生件、`demo/`（被 runs 取代）、`smoke_summary.json`、旧 `printable.3mf`、错误尺寸的 `official.*`（可随 --export 再生）、`repaired.glb` 中间件；保留 `assets/` 源 GLB + 两个完整交付组（runs/ahri_figurine、runs/headphone_hook）。

- **v0.2 官方导出链 PASS**：Asset SDK 上传（COS）→ multi-format-export → 下载 STL(3.8MB)/3MF(0.9MB)，官方 STL 水密 ✓。踩坑：SDK v1.9 签名是 `create_*_client(AholoClientConfig(api_key, region))`，文档页的 `region=` kwarg 写法会 TypeError；**待查（v0.3）**：官方导出件 bbox 与本地修复件有偏差（本例最大边 81 vs 期望 ~100mm，疑服务端重处理/轴约定），交付尺寸以实测为准；
- **v0.2 交互 HTML 报告 PASS**：plotly 内嵌（6.1MB 自包含，离线双击可开），指标面板 + 缺陷着色 3D + 薄壁标记；CLI `report` 子命令 + pipeline 自动产出；
- **printguard skill 已注册**：`.dsh/skills/printguard/SKILL.md`（watch 机制免重启进目录）。硬规则：未给尺寸先问、生成串行、壳汤先修复、导出失败不重试烧配额、修不动如实说。

- **v0.3 壁厚修复 PASS**：体素域增厚（薄壁中轴定位 → 局部膨胀回贴，r 封顶 4 voxel，代价=薄壁区轮廓外扩 ~r*pitch）；selftest 6 项全过（thin_plate 0.60mm fail → +0.12mm/side → 0.84mm warn，风险 C→S）；不可修时报 `wall_unrepairable` + 建议重设计；
- **v0.3 bbox 偏差已定位 + 尺寸契约 PASS**：偏差根因 = 官方导出服务端各向同性 ×0.808（两次复现），非本地几何问题；对策三件套：repair 新增摆向后 `scale_to(height_mm)` 重缩放（实测本地 printable.stl 最大边 100.0 ✓）、官方产物改名 `official.*`（防覆盖本地件）、pipeline 自动比对官方/本地尺寸比并 warning；
- **v0.3 "太粗糙"诊断（证据齐）**：① G1+300k vs Turbo+100k A/B——面数 3 倍、同尺度表面积 +15%（真实细节）、生成 292s vs 46s；② 重建 pitch 0.3 vs 0.81 表面积完全一致（42% 差额是**正确移除**壳汤内部面，非细节毁灭）——但**粗 pitch 会静默抹掉 0.3mm 级小特征**（0.81 下尾巴尖直接消失、连警告都不发）；③ 交付体积解 = 3MF 压缩（143MB STL→54MB）。结论落地为 `--quality fast|fine` 双模式（pipeline 已实现；G1 下载含 ZIP+GLB 双文件的坑已修——按真实扩展名命名）；
- **v0.3 产品形态落地：pip/uvx 可安装包**：`pyproject.toml`（元数据/依赖/入口 `printguard-mcp`）+ README 三步接入（uvx 从 GitHub 自动拉包，用户零手动下载；key 走 MCP 配置 env，检测/修复可免 key）；官方 stdio 客户端对**打包后入口**协议验证 PASS（list_tools + call_tool）；`lux3d_client.load_key` 改为 env 优先；
- **v0.3 交付收敛 + 图文输入（已验证）**：交付物精简为 `printable.3mf` + `diagnostic_sheet.png`（左=缺陷着色 3D、右=中文指标面板与修复步骤，替代 HTML/STL 散件）+ `turntable.gif`（旋转一周的"准 3D"对话内预览）；`--image`/MCP `image` 参数支持**图+文同时输入**（本地图自动走 Asset 上传，真实生成已验证）；修复三小坑（GIF append 需 PIL Image、面板字体改雅黑避免方框、main 退出码键名）；渲染加 **Lambert 头灯着色**（面法线×相机方向调亮度）——统一灰会完全失去立体感（用户实测反馈后修复，诊断图与 GIF 均已应用）；
- **v0.3 MCP Server 落地 PASS**：`mcp_server.py`（FastMCP，stdio），复用 `run_pipeline / deliver_outputs`（pipeline 已重构为 CLI/MCP 双入口）；官方 stdio 客户端协议验证 PASS（list_tools + call_tool 全链路，测试脚本 `test_mcp_client.py` 保留作回归）。宿主配置示例见「MCP 接入」。踩坑：mcp SDK 2.x 把 FastMCP 改名 MCPServer，已钉 `mcp<2`；沙箱禁管道，协议测试需提权一次（已知边界）。**接口演进**：同步工具（generate/repair_printable）→ **异步 job 模式**（start_printguard_job + check_printguard_job + analyze_printability），生成/修复耗时长，宿主免阻塞；产物自动上传 COS 以 URL 返回；
- **v0.3.1 群核三函数实测 PASS**：`concept_image` ✓（31s，纯 prompt 出概念图）；`material_transfer` ✓（262s，白模猫+陶瓷概念图 → 釉面材质 GLB，渲染链验证贴图/着色正常）；`part_split` ✓ 链路通（任务+下载+GLB 有效 29.9 万面）——单连体网格返回原样 1 件（拆件器合理决策），语义化"拆成 N 件"需多部件输入，如实记录。三函数保留在 `lux3d_client` 备用，未接入交付链；
- **v0.3.1 wall_thicken v2（闭环收敛）+ 疑点破案**：插桩复现定位根因——v1 一次性开环膨胀在细 pitch 下**残留 fail 边缘特征**（增厚后网格直接量测仍有 7 个 fail 中轴体素），且 marching-cubes 往返量化会把亚 pitch 残余再退化一档（0.49→0.27）；v2 改为**探→胀→再探闭环**（≤3 轮，目标 T≥0.8+2×pitch 安全裕量，预算超限如实报"发丝级细节建议重设计或接受"），selftest 全过（thin_plate fail→warn 0.84）。生产复现（ahri fine 档）证实：**发丝级尾尖/毛细节物理上超出体素增厚可救范围**（+1.08mm/side 预算吃满仍有残余，且继续加厚会把毛发变棒状毁设计）——诚实报告+建议重设计是正确行为，非 bug；
- 调试教训：调试脚本被 import 时顶层实验代码会重跑（多烧 2 次渲染配额）——可复用逻辑一律提取进正式模块函数，调试脚本只做参数化调用；
- **v0.4 结构简化 + MCP 去托管**：MCP 仅保留 stdio（删 streamable-http / `--transport` / `--host` / `--port`），产物不再传 COS 换 URL，直接返回本地路径（宿主与本机同侧，路径即所得）；官方导出链整体移除（`lux3d_client.export_official`、pipeline `--export`、MCP `export_official` 参数及尺寸比告警——×0.808 服务端偏差两次复现，本地 3MF 恒为打印基准；Asset SDK/`upload_mesh` 保留供 `--image` 参考图上传）；删除被 diagnostic_sheet 取代的旧渲染（`render_report`/`build_html_report` + CLI `render`/`report` 子命令，连带移除 plotly 依赖）与 lux3d_client 冒烟残留（`PROMPTS`/`main`/`analyze_raw`）；`download_glbs` 去掉未用的 key 参数；MCP 修复分支提取为 `pipeline.run_repair`（CLI/MCP 复用），生成失败显式置 error 不再静默交付空结果；`test_mcp_client` 回归目标改 `output/healthy_cube.stl`（原 `printable.stl` 已随工作区清理删除）。保留：异步 job 模式、概念图/拆件/材质迁移、fast/fine 双档。四模块 1466 → ~1030 行。
- **v0.4 顺手修掉一个潜伏 bug**：`_solid_grid` 对**大面片网格**（低多边形盒子等，单三角形边长远大于 pitch）做 `voxelized()` 时，subdivide 细分轮数超 trimesh 默认 `max_iter=10` 直接抛 "max_iter exceeded"——此前测试目标全是 marching-cubes 细碎三角形故未暴露，被新 MCP 回归目标（合成盒子 STL@80mm）撞出。修复：`voxelized(pitch, max_iter=64)`；selftest 增至 7 项（新增 big_facet_reload：大面片 STL 重载→80mm→wall pass）。
- **v0.4 开源发布（v0.4.0 → github.com/milu348/PrintGuard_Aholo）**：git init 首推（main + tag v0.4.0），远程原占位 README 经 --force-with-lease 替换；新增 MIT LICENSE（署名 PrintGuard contributors）与 docs/img 示例图（README 交付示例节，九尾狐 fast 档案例）；test_mcp_client ROOT 改 __file__ 相对（clone 即可跑）、PrintGuard.md MCP 配置示例路径通用化；情报汇总与全部运行产物不入库（.gitignore 核查 CLEAN）。打包冒烟：setuptools 由 pyproject 构建 wheel 成功（printguard-0.4.0，24KB，四模块+入口），uvx 安装链路打包层验证通过。沙箱注意：pip 的 build 子进程写临时目录会被沙箱拦（git/pwsh cmdlet 不受影响），构建验证需提权一次；venv 无 pip（--without-pip 遗留）时用全局 python -m pip。
- **v0.4 Agent 工具样式对齐（无服务器读取方案落地）**：产物改**绝对路径**（uvx 分发时宿主 cwd 不确定的兜底）；任务 done 时 `check_printguard_job` **内联返回诊断缩略图** `sheet_preview.jpg`（PIL 降采样 JPEG，实测 11KB，全尺寸 PNG 67KB 留路径）——agent 在对话内直接"看"诊断图讲解缺陷，零外部服务器（与 playwright 截图同款协议机制）；`quality`/`style` 改 Literal 枚举（JSON Schema 自动带 enum）；`FastMCP(instructions=...)` 随 initialize 下发四条硬规则；错误统一 raise（宿主收 is_error）。**顺手修掉第二个潜伏 bug**：`prompt` 原为 schema 必填 → 不带 prompt 的修复任务从来过不了 pydantic 校验（旧测试未覆盖 repair 路径）；改为可选 + 函数体显式校验，`height_mm` 同理——schema 必填会逼宿主 LLM 编数字，违反"绝不猜尺寸"，显式报错顶回去问用户才是正确契约。真机回归：repair job 全链路 INLINE PASS（done 块=['text','image']，四路径均为绝对值）。
- **v0.5 P1 检测项评估：实现 → 验证 → 决策暂缓（决策过程完整留档）**：#4 间隙 / #5 小孔 / #6 密闭腔的检测与修复全部实现并跑过 selftest（8/10），收尾前做产品价值复盘，结论是**整体撤回**（printguard.py 回滚至 v0.4.0 基线）。理由：① 对目标场景（AI 生成手办/结构件）三项均非打印失败主因——P0（水密/壁厚/悬垂）才是翻车重灾区；② 小孔缩径的创客常规解法（打印后钻孔）成本低于预防；③ 密闭腔用切片器 100% 填充零成本规避，自动钻排气孔反而明显改几何；④ 间隙检测受体素分辨率硬限制（≈2×pitch，130mm 模型上 ≈0.54mm，**大于 0.4mm 阈值本身**），verdict 天然偏软，混进报告会稀释四个硬指标的公信力；⑤ 自动修复价值/风险比不划算（gap_fuse 焊死窄缝会改设计意图、扩孔 +0.06mm 不如建模端 +5% 精确）。**过程中撞出的已知盲区（留档，暂不修）**：壁厚分析网格经 fill_holes，空心件（省材料打印的常见做法）会被当实心、壁厚虚报为整体尺寸——修复方案已验证可行（分析网格 fill=False，~4 行，含空心盒正确报 5mm 壳厚的 selftest 已跑通），待后续与空心件场景需求一起评估。
- **v0.5 功能收敛（第二轮删减）**：`material_transfer`/`part_split` 从 lux3d_client 移除（与核心主张无关且拆件对单连体 AI 生成件无效，git 历史可找回）；`concept_image` 保留——它是完整实现且实测通过的独立函数（31s 出图），可直接导入调用，无需额外 API 代码，是否接入交付链留待后续决策。B 类未验证项（uvx 端到端/MCP 生成 job/图文输入/URL 输入分支）本轮决策暂不验证，留档待测。
- **v0.4.1 产品流程对齐（用户定义最终流程）**：用户对话 → Lux3D 生成（模型文件）→ PrintGuard 可制造校验+修复（核心工作，渲染与诊断均本地生成——**Lux3D API 不出渲染图，outputs 实证仅 GLB/ZIP**）→ 交付收敛为 **printable.3mf + turntable.gif**；diagnostic_sheet.png/sheet_preview.jpg 转为内部诊断件（agent 内联查看讲解修复用，不作交付承诺，磁盘仍生成）。MCP 结果 files 仅含 3MF+GIF，instructions 同步更新。


## MCP 接入（已落地）

任何 MCP 宿主（Claude Desktop / Cursor / DSH…）加一段配置即得能力：

```json
{
  "mcpServers": {
    "printguard": {
      "command": "<克隆路径>\\.venv\\Scripts\\python.exe",
      "args": ["<克隆路径>\\mcp_server.py"],
      "cwd": "<克隆路径>"
    }
  }
}
```

| 工具 | 入参 | 说明 |
|---|---|---|
| start_printguard_job | prompt（生成必填）或 file_or_url（修复），height_mm(必填), quality(fast/fine), style, image | 提交即返回 job_id，不阻塞宿主 |
| check_printguard_job | job_id | 轮询状态；done 时返回产物绝对路径 + 内联诊断缩略图（ImageContent） |
| analyze_printability | file_or_url, height_mm(必填) | 只检测，秒级同步返回（免 key） |

v0.4 起 Agent 工具样式对齐：`instructions` 随 initialize 下发四条硬规则（先问尺寸 / 生成串行 / 3MF 为基准 / C-D 先告知）；`quality`/`style` 为 enum schema；`height_mm` 故意不做 schema 必填——缺参会显式报错把宿主顶回去问用户，避免 LLM 为过校验编数字；任务 done 时内联返回诊断缩略图（`sheet_preview.jpg`，实测 ~11KB）+ 全部产物绝对路径；stdio 仅限桌面宿主（Claude Desktop / Cursor / DSH 等本机运行时），全程零外部服务器。

## 技术风险

| 风险 | 等级 | 对策 |
|---|---|---|
| ~~壁厚"偏置增厚"实现难度高~~（已落地尽力而为版） | 中 | 体素域局部膨胀（selftest 0.60→0.84mm）；修不动报 wall_unrepairable+建议重设计；已知代价=薄壁区轮廓外扩 ~r*pitch |
| ~~Lux3D 原始尺度/网格质量假设未验证~~（已解除） | - | 冒烟测试完成：尺度=归一化（`--height-mm` 必选）、mesh=自交壳汤（体素重构为必选路径），详见「API 要点」实证 |
| ~~官方导出件尺寸 ×0.808（服务端行为，复现两次）~~（已随官方导出链移除，v0.4） | - | 本地 `printable.3mf` 恒为打印基准 |
| 摆放优化与"平底朝下"目标冲突 | 低 | 目标函数加权合并（支撑体积 + 床面接触 + 悬垂），支持手动指定摆放 |

## 实施顺序

| 阶段 | 内容 | 退出条件 |
|---|---|---|
| M1 环境与数据 | 建 `.venv`；装依赖；**API 冒烟测试**（鉴权/原始尺度/mesh 质量）；测试集 = 程序化损伤合成器（钻孔/削薄/拉伸出已知缺陷）+ 真实生成模型 | analyze 对测试集跑通，报告 JSON 结构（含风险等级、before/after 指标字段）定稿 |
| M2 检测（P0） | 水密性、壁厚（体素 EDT 中轴法）、悬垂检测；matplotlib 着色 + plotly HTML | 测试集全部出正确诊断 + 高亮图 |
| M3 修复（P0） | 体素重构填洞、增厚（尽力而为）、自动摆向 | 修复后 mesh 复检通过，前后对比图自动产出 |
| M4 生成接入 | 自研 lux3d 客户端，接入 DSH skill 指令（编排、报告话术、追问如"拆两件"→part-split、未给尺寸先询问） | 一句话 → STL/3MF + HTML 报告全流程跑通 |
| M5 扩展 | P1/P2 检测项、预生成资产池、（可选）MCP 封装、headless 切片实测 | 全流程稳定复现 3 次无故障 |

> **实际进度**：M1–M4 完成；M5 部分完成（MCP 封装 ✓、资产池 ✓、fast/fine 双档 ✓；P1/P2 检测项与切片实测未做——P1 经 v0.5 评估后决定暂缓，见开发日志）。
