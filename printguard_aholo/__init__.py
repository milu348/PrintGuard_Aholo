"""PrintGuard_Aholo —— FDM 打印件的「生成 → 体检 → 修复 → 交付」工具链。

模块地图（v0.2 分层结构，依赖方向单向）：
- ``config``          Settings 数据类：环境变量只在 from_env() 读取一次
- ``core/``           几何内核（纯计算、零服务依赖、离线可用）
  - ``profiles``      打印机 profile 与阈值
  - ``meshio``        网格加载/尺寸归一
  - ``voxel``         体素化底层算子与内存护栏
  - ``inspect``       几何体检（水密 / 悬垂 / 壁厚——只留驱动修复的三项）
  - ``repair``        几何修复（重构/平滑/增厚/摆向）
  - ``selftest``      合成用例回归
- ``lux3d/``          Lux3D 开放平台（生成层）
  - ``client``        任务创建（文生/图生/文图混合）、轮询、查询、下载
  - ``asset``         Asset 上传链（图片 → 干净公网 URL）
- ``pipeline/``       一条龙编排
  - ``generate``      生成入口（三种模式路由）
  - ``repair_run``    修复入口
  - ``execute``       执行机械（校验/run目录/修复编排/导出）
  - ``inputs``        输入归一化（URL/本地/base64）
  - ``delivery``      限时交付（内存取件 + 限时链接）
- ``service/``        服务层
  - ``jobs``          单 worker 异步任务队列
  - ``app``           build_app(Settings)：显式装配 FastMCP + 队列 + 存储
  - ``tools``         7 个 MCP 工具
  - ``web``           核心 HTTP 路由（/healthz、/d）
- ``server``          入口：Settings → build_app → 运行
- ``cli``             printguard 命令行

可选演示层（独立顶层包，核心不感知，组合根按需挂载）：
- ``modelscope_agent`` 网页 Agent：聊天页 + LLM function calling（复用同一批工具）
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
