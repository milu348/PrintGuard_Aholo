---
name: printguard
description: PrintGuard——FDM 打印件的"生成→可制造性校核→修复→交付"工具链（Lux3D 生成 + 几何检测修复 + STL/3MF 交付 + 诊断报告）。当用户想生成可 3D 打印的模型、检查/修复 mesh 能否打印、要可打印文件或诊断报告时使用。
whenToUse: 用户提到 3D 打印、FDM、可打印、修模型、STL/3MF、切片、打印失败、Lux3D 生成实物、手机支架/耳机架等实物件时触发。
---

# PrintGuard 使用指令

工作区即工具箱（本 skill 所在项目根）：

- **环境**：一律用 `.venv\Scripts\python.exe`（勿用全局 python，依赖只在 venv 里）
- **一条龙**：`pipeline.py "描述" --height-mm 100 [--quality fast|fine] [--style cartoon] [--from-file <glb>]`
  → 生成→检测→修复→交付 `printable.3mf` + `turntable.gif`（渲染预览）；诊断图为内部件（agent 讲解用）+ `report.json`
  - `--quality fast`（默认）：G1-Turbo+100k+自适应 pitch，全程 ~2min，结构件/快速迭代
  - `--quality fine`：G1+300k+0.3mm 细 pitch，~10min、文件大（交付以 3MF 为准），**手办/细节件必用**——G1 细节 +15%，且 0.8mm 粗 pitch 会静默抹掉 0.3mm 级特征（细 pitch 下薄壁/尾巴尖的警告更多也更真实）
  - `--from-file`：复用已有 GLB 跳过生成（测试/资产池）
- **检测**：`printguard.py analyze <file> --height-mm 80`
- **修复**：`printguard.py repair <file> -o out.stl --height-mm 80 [--no-orient]`
- **概念图**：`lux3d_client.concept_image(key, prompt)`（生成类任务，与生成同串行规则；已实测 31s 出图，未接入交付链，可直接导入调用）
- **自测**：`printguard.py selftest`（8 个检查用例，改动检测/修复逻辑后必跑）

## 硬规则

1. **用户未给尺寸必须先问**（单位 mm），绝不替用户猜尺寸——所有工艺阈值以真实毫米为准。
2. Lux3D 生成类任务并发=1，必须串行（生成/概念图/材质迁移/拆件互相排队）；结果 URL 2 小时有效，下载后立即处理。
3. 非水密（壳汤）输入的壁厚无意义——先修复再复检（工具已自动跳过，不要绕过）。
4. 修复哲学：自动修 + 报告科普；修不动的（如严重薄壁）如实告知"需重新设计"并给具体建议，不假装能修。

## 报告解读话术

- risk **S/A**：可放心打印；**B**：建议看悬垂/壁厚明细；**C/D**：逐项列问题后再交付。
- 悬垂指标已含向下支撑探测：open_frac=真悬空需支撑，bridged=桥接/贴材料（无需支撑）；话术用"约 X% 表面真正悬空"。
- 壁厚 **warn** 是量化保守带（±2*pitch），**fail** 才需要动作（加厚或重设计）。
- 交付时给用户：`printable.3mf` 路径 + `turntable.gif`，并简述"修了什么、为什么"。

## 已知边界

- 悬垂为几何法+体素向下探测（10mm）；体素重构有损（pitch 量级）；曲面壁厚 warn 偏保守。
- 真实 Lux3D 输出是"自交壳汤"（非流形边数百），体素重构是必需修复路径，非 bug。
- 中文路径下 venv 的 ensurepip 会失败：用 `--without-pip` + 全局 `pip --python` 安装。