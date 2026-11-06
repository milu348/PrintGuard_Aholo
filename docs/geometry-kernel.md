# 几何内核（检测 / 修复）

从上一版单文件实现拆分而来，算法与判定口径保持不变，只做了模块化、
服务化改造（不写工作目录文件、可选依赖延迟导入、去掉 CLI 副作用）。

## 模块地图

| 模块 | 职责 |
|---|---|
| `printguard_aholo/profiles.py` | `PrinterProfile`、内置 profile、阈值常量 |
| `printguard_aholo/meshio.py` | 网格加载（含 `height_mm` 尺寸归一） |
| `printguard_aholo/voxel.py` | 表面采样体素化、实心化、内存护栏 |
| `printguard_aholo/inspect.py` | 水密性 / 悬垂 + 支撑探测 / 壁厚 / 孔 / 孤岛 / 平整度 / 风险评级 |
| `printguard_aholo/repair.py` | 碎片清理 / 体素重构 / Taubin 平滑 / 壁厚增厚 / 自动摆向 |
| `printguard_aholo/selftest.py` | 6 个合成模型 + 2 个回归用例 |
| `printguard_aholo/cli.py` | `run` / `analyze` / `repair` / `selftest` 命令行 |

依赖方向是单向的：`profiles → voxel → inspect → repair → selftest/cli`。

## 用法

```python
import json
from printguard_aholo.meshio import load_mesh
from printguard_aholo.inspect import analyze_mesh
from printguard_aholo.repair import repair

mesh = load_mesh("model.glb", height_mm=120)   # GLB 单位不可信时必须给 height_mm
report = analyze_mesh(mesh)
print(report["risk"])                          # {'grade': 'B', 'score': 74.0}

fixed, steps, before, after = repair(mesh, do_orient=True)
fixed.export("fixed.stl")
print(json.dumps({"steps": steps, "after": after["risk"]}, ensure_ascii=False))
```

CLI：

```bash
printguard-geometry analyze model.stl --height-mm 120 --json analysis.json
printguard-geometry repair  model.stl -o fixed.stl --json analysis.json
printguard-geometry selftest            # 8 用例，约 90 秒
```

## 判定口径

| 项 | 方法 | 判定 |
|---|---|---|
| 水密性 | 边计数 | 边界边 / 非流形边 / 退化面 |
| 悬垂 | 法线朝下 > 阈值且非贴床 | 面积占比；再沿重力向下探 10mm 区分「真悬空 / 桥接」 |
| 壁厚 | 体素 EDT 中轴 + 射线精测 | `fail`（≤ 阈值−pitch）/ `warn` / `pass` |
| 孔 | 背景 EDT + 连通域 + 环绕射线 | 内径 < `min_hole_mm` 记 `small_holes` |
| 孤岛 | 连通体拆分 | 体积 < `island_min_mm3` 记碎片 |
| 平整度 | 相邻面二面角分布 | `frac_gt30` 用于验证去阶梯效果 |
| 风险 | 加权扣分 | S / A / B / C / D |

默认 `generic-0.4`（0.4 喷嘴、0.2 层高、0.8mm 最小壁厚、45° 悬垂）；
`mini-0.2` 为 0.2 喷嘴。自定义 profile 可传 JSON 路径，未写字段回退同名内置。

## 修复链路

1. `repair_island_clean`：删除小于阈值的碎片孤岛；
2. 非水密 / 有非流形边 → `repair_voxel`（体素重构，必然有损）；
3. `smooth_taubin`：消除 marching cubes 的 45° 阶梯（p95 折角 45° → 14° 量级）；
4. `repair_thicken`：薄壁区域闭环增厚，预算 4 体素，超预算判「建议重新设计」；
5. `orient`：候选姿态里选悬垂占比最小、贴床面积最大者。

**顺序不能换**：平滑必须在摆向之前，否则阶梯伪法线会污染悬垂判断。

## 已知局限

- 悬垂采用几何法，堆叠贴合面靠「向下支撑探测」豁免；探测距离固定 10mm；
- 体素重构有损，细节被磨平到 pitch（默认最大边/160，0.4–2.0mm）；
- 壁厚是体素量化估计（`T = 2D − pitch`），薄弱区才做射线精测；
- 孔检测只覆盖内径判定，桥接 / 小孔 / 孤岛切片级分析仍未实现；
- 自动摆向只枚举 6 个轴对齐 + 凸包大面候选，不保证全局最优。

## 部署注意

- 运行依赖：`numpy`、`trimesh`、`scipy`、`scikit-image`（marching cubes）、`rtree`（射线）、`lxml`（3MF 导出）；
- 大体积累加会吃内存：`PRINTGUARD_MAX_VOXELS`（默认 1.5e8）可收紧，
  pitch 会自动放粗（精度换内存）。

## 回归

```bash
# 快速（约 30 秒，全部离线用例）
.venv/bin/python -m unittest discover -s tests -t .

# 完整 8 用例（约 90 秒）
PRINTGUARD_SLOW_TESTS=1 .venv/bin/python -m unittest tests.test_geometry_offline
# 或
.venv/bin/python -m printguard_aholo.cli selftest
```
