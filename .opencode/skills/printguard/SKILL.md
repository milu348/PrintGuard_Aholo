---
name: printguard
description: PrintGuard_Aholo——FDM 打印件的"生成→可制造性校核→修复→交付"工具链（Lux3D 生成 + 几何检测修复 + STL/3MF 交付）。当用户想生成可 3D 打印的模型、检查/修复 mesh 能否打印、要可打印文件时使用。
whenToUse: 用户提到 3D 打印、FDM、可打印、修模型、STL/3MF、切片、打印失败、Lux3D 生成实物、手机支架/耳机架等实物件时触发。
---

# PrintGuard_Aholo 使用指令

工作区即工具箱（本 skill 所在项目根，Linux / WSL）：

- **环境**：一律用 `.venv/bin/python` 或 `.venv/bin/printguard`（勿用全局 python，依赖只在 venv 里）
- **一条龙**：`printguard run "描述" --height-mm 100 [--quality fast|fine] [--style cartoon] [--from-file <glb>]`
  → 生成→检测→修复→交付 `printable.3mf`（+ Lux3D 概念图/四视图，若有输入图）
  - 服务端**不生成诊断报告**（无 report.md / report.json），结果只回
    `ok / run_id / mode / artifacts / warnings / duration_sec`
  - `--quality fast`（默认）：G1-Turbo+100k+自适应 pitch，全程 ~2min，结构件/快速迭代
  - `--quality fine`：G1+300k+0.3mm 细 pitch，~10min、文件大（交付以 3MF 为准），**手办/细节件必用**
  - `--from-file`：复用已有 GLB 跳过生成（测试/资产池）
  - `--no-smooth`：跳过去阶梯平滑（默认开）。体素重构产物必带 45° 台阶面，只有用户
    明确要「原始体素棱面」时才关
- **检测（命令行）**：`printguard analyze <file> --height-mm 80 --json analysis.json`
- **修复（命令行）**：`printguard repair <file> -o out.stl --height-mm 80 [--no-orient]`
- **概念图 / 四视图**：MCP 工具 `lux3d_concept_image` / `lux3d_four_view`，或 `Lux3DClient`
- **自测**：`printguard selftest`（8 个检查用例，改动检测/修复逻辑后必跑）

MCP 工具（9 个）：`lux3d_concept_image` / `lux3d_four_view` / `lux3d_text_to_3d` /
`lux3d_image_to_3d` / `lux3d_get_task` / `lux3d_list_tasks` /
`printguard_run` / `printguard_repair` / `printguard_check_job`。

## 硬规则

1. **用户未给尺寸必须先问**（单位 mm），绝不替用户猜尺寸——所有工艺阈值以真实毫米为准。
2. Lux3D 生成类任务并发=1，必须串行（生成/概念图/材质迁移/拆件互相排队）；结果 URL 2 小时有效，下载后立即处理。
3. 非水密（壳汤）输入的壁厚无意义——先修复再复检（工具已自动跳过，不要绕过）。
4. 修复哲学：自动修；修不动的（如严重薄壁）如实告知"需重新设计"并给具体建议，不假装能修。
5. **API Key 由用户提供**：云端部署时用户把 AHOLO key 填在 `api_key` 工具参数里，绝不猜测/编造 key；本地部署可走环境变量 `AHOLO_API_KEY`。

## 交付说明

- run 目录：`printable.3mf`（或回退 `.stl`）+ 可选 `concept_image.*` /
  `four_view_XX.*` / `reference_image.*`；**没有诊断报告、没有本地渲染图**。
- 3D 生成开放 API 只返回 GLB，没有模型预览图；交付图只来自概念图 / 四视图。
- 交付时给用户：模型路径或 `/files/<run_id>/<filename>` 下载链接，并简述"修了什么"。
