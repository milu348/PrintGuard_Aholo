# 几何内核（检测 / 修复）

从上一版单文件实现拆分而来，算法与判定口径保持不变，只做了模块化、
服务化改造（不写工作目录文件、可选依赖延迟导入、去掉 CLI 副作用）。

## 模块地图

| 模块 | 职责 |
|---|---|
| `printguard_aholo/core/profiles.py` | `PrinterProfile`、内置 profile、阈值常量 |
| `printguard_aholo/core/meshio.py` | 网格加载（含 `height_mm` 尺寸归一） |
| `printguard_aholo/core/voxel.py` | 表面采样体素化、实心化、内存护栏 |
| `printguard_aholo/core/inspect.py` | 水密性 / 悬垂 + 支撑探测 / 壁厚（只留驱动修复的三项） |
| `printguard_aholo/core/repair.py` | 碎片清理 / 体素重构 / Taubin 平滑 / 壁厚增厚 / 自动摆向 |
| `printguard_aholo/core/selftest.py` | 6 个合成模型 + 2 项回归（GLB 往返、大面片重载） |
| `printguard_aholo/cli.py` | `run` / `analyze` / `repair` / `selftest` 命令行 |

依赖方向是单向的：`profiles → voxel → inspect → repair → selftest/cli`；
core 包不依赖任何平台 / 服务代码（离线可用是硬约束）。

## 用法

```python
import json
from printguard_aholo.core.meshio import load_mesh
from printguard_aholo.core.inspect import analyze_mesh
from printguard_aholo.core.repair import repair

mesh = load_mesh("model.glb", height_mm=120)   # GLB 单位不可信时必须给 height_mm
report = analyze_mesh(mesh)
print(report["checks"]["watertight"])           # {'watertight': True, ...}

fixed, steps, before, after = repair(mesh, do_orient=True)
fixed.export("fixed.stl")
print(json.dumps({"steps": steps, "wall": after["checks"]["wall_thickness"]["verdict"]},
                  ensure_ascii=False))
```

CLI：

```bash
printguard analyze model.stl --height-mm 120 --json analysis.json
printguard repair  model.stl -o fixed.stl --json analysis.json
printguard selftest                     # 6 个合成模型 + 2 项回归
```

## 判定口径

| 项 | 方法 | 谁在用 |
|---|---|---|
| 水密性 | 边计数（边界边 / 非流形边 / 退化面 / 体积） | ✅ 驱动体素重构（`repair` 只读这一项） |
| 悬垂 | 法线朝下 > 阈值且非贴床（面积占比） | ✅ 驱动自动摆向（`orient` 选姿态） |
| 悬垂支撑 | 悬垂面沿重力在 raw 体素网格向下探 10mm，区分「真悬空 / 桥接」 | ✅ 回归断言：堆叠贴合面不得误判为悬垂 |
| 壁厚 | 体素 EDT → 中轴 → T = 2D − p；薄弱区射线精测 | ✅ 驱动薄壁增厚（`repair_thicken`） |

**只服务「修复决策」的才留**。孔洞、孤岛、平整度、风险评级曾是报告字段，服务与 CLI 都不消费，已删除；平整度的数学挪进 `selftest._roughness`，作为 Taubin 平滑是否生效的回归断言（阈值 0.05）。

## 修复链路

1. `repair_island_clean`：删除小于阈值的碎片孤岛；
2. 非水密 / 有非流形边 → `repair_voxel`（体素重构，必然有损）；
3. `smooth_taubin`：消除 marching cubes 的 45° 阶梯（p95 折角 45° → 14° 量级）；
4. `repair_thicken`：薄壁区域闭环增厚，预算 4 体素，超预算判「建议重新设计」；
   **比例原则**：fail 体积占模型体积 <0.5% 时跳过增厚——微小薄弱点只报告不改形，
   避免为千分之几的缺陷重建整个表面（实测教训：0.001% 的薄弱点触发重建 + 降密，
   交付表面出现波纹劣化）；
5. `orient`：候选姿态里选悬垂占比最小、贴床面积最大者。

**顺序不能换**：平滑必须在摆向之前，否则阶梯伪法线会污染悬垂判断。

## 已知局限

- 悬垂采用几何法，堆叠贴合面靠「向下支撑探测」豁免；探测距离固定 10mm；
- 体素重构有损，细节被磨平到 pitch（默认最大边/160，0.4–2.0mm）；
- 壁厚是体素量化估计（`T = 2D − pitch`），薄弱区才做射线精测；

- 自动摆向只枚举 6 个轴对齐 + 凸包大面候选，不保证全局最优。

## 部署注意

- 运行依赖：`numpy`、`trimesh`、`scipy`、`scikit-image`（marching cubes）、`rtree`（射线）、`lxml`（3MF 导出）；
  （孔洞检测的射线逻辑随 `hole_stats` 一并删除，`rtree` 目前仍被壁厚薄弱区精测使用）
- 大体积累加会吃内存：`PRINTGUARD_MAX_VOXELS` 是**硬预算**，pitch 会自动放粗到
  `(ext/pitch)³ ≤ 预算`（`voxel.fit_pitch`）。过去写成「超限就放粗，但 pitch 不超过
  0.6mm」——撞到上限就放弃，500mm 件仍会建 5.8 亿体素被 OOM kill；现已改为预算优先。
- 实测内存 ≈ **体素数 × 60B**：`distance_transform_edt` 内部同时持有多份同尺寸
  float64/int 工作数组，28M 体素单次调用就 +1.37GB。150mm 水密件、服务链路
  （`repair(analyze=False)`）实测：

  | 预算 | pitch | 耗时 | 峰值 RSS |
  |---|---|---|---|
  | 3.5e7 | 0.49mm | 10.6s | 1734MB |
  | 2e7（托管默认） | 0.66mm | 5.4s | **872MB** |
  | 1e7 | 0.90mm | 3.3s | 484MB |
  | 1.5e8（桌面默认） | 0.20mm | — | ~6GB，2GB 容器必 OOM |

- 服务链路不算体检（`repair(analyze=False)`）——一次 `analyze_mesh` 在 150mm 件上
  另需 ~0.8GB（只剩壁厚 EDT + 支撑探测），只留给 CLI `analyze` 与 selftest。
- `repair(analyze=False)`：管线/服务不消费体检结论，跳过前后两次 `analyze_mesh`
  （真实 GLB 实测省 74s + 数 GB）；CLI `--json` 与 selftest 仍按默认 True 全算。

## 回归

```bash
# 快速（约 30 秒，全部离线用例）
.venv/bin/python -m unittest discover -s tests -t .

# 完整 8 用例（约 90 秒）
PRINTGUARD_SLOW_TESTS=1 .venv/bin/python -m unittest tests.test_geometry_offline
# 或
.venv/bin/python -m printguard_aholo.cli selftest
```
