"""服务层：MCP 装配、工具注册、HTTP 路由、异步队列。

模块地图（依赖方向单向：jobs → app → tools/web → server）：
- ``jobs``   单 worker 异步任务队列（服务内唯一实现，MCP/Agent 各持一个实例）
- ``app``    `build_app(Settings)`：显式装配 FastMCP + 队列 + 交付存储，
             并触发工具 / 路由注册——import 本包不再产生任何副作用
- ``tools``  MCP 工具（生成层 / 管线层 / 任务查询），返回工具注册表
- ``web``    核心 HTTP 路由（/healthz、/d/<token>）

网页 Agent 演示层（聊天页 + /api/chat + /api/job）在独立包
``modelscope_agent``——由组合根按需挂载，本包不感知、不 import 它。

入口仍是包根的 ``server.main()``：`Settings.from_env()` → `build_app()` → 运行。
"""
