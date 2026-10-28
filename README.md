# PrintGuard

面向 FDM 打印件的 **"生成 → 可制造性校核 → 修复 → 交付"** MCP 能力包。
Lux3D 生成模型 + 确定性几何工具链（水密/悬垂/壁厚检测，体素重构/增厚/自动摆向），
交付可直接切片的 3MF、诊断报告图（缺陷着色+中文指标）与旋转预览 GIF。

> 运行环境：**Linux / WSL**（Python ≥ 3.11，推荐 3.13）。不再兼容 Windows。

## 交付示例

九尾狐手办（anime / G1-Turbo / 130mm，fast 档端到端 ~2min）：风险 D→S，水密，悬垂 16%→5%。
诊断图左=缺陷着色 3D（灰=正常 · 蓝=贴床 · 红=悬垂 · ◆=最薄壁），右=中文指标与修复步骤；GIF 为对话内旋转预览。

![诊断报告图](docs/img/diagnostic_sheet.png)

![旋转预览](docs/img/turntable.gif)

## MCP 接入（Server Config）

两种接入方式，工具完全一致：**方式一**标准接入（本机运行，uvx 从 PyPI 自动安装）；
**方式二**免安装，直连魔搭创空间上已部署的实例。

### 方式一：标准接入（stdio，uvx 从 PyPI 自动安装）

在支持 MCP 的客户端（Claude Desktop / Cursor / OpenCode 等）配置中添加：

```json
{
  "mcpServers": {
    "printguard": {
      "command": "uvx",
      "args": ["printguard-aholo@latest"],
      "env": {
        "AHOLO_API_KEY": ""
      }
    }
  }
}
```

- 首次调用时 uv 自动从 PyPI 安装（需先安装
  [uv](https://docs.astral.sh/uv/)：`curl -LsSf https://astral.sh/uv/install.sh | sh`）
- 环境变量 `AHOLO_API_KEY` 可留空：检测/修复已有模型完全不需要 key；仅生成新模型需要
  （免费申请：labs.aholo3d.cn），也可在每次工具调用的 `api_key` 参数中提供
- 产物以本地绝对路径交付

### 方式二：云端直连（Streamable HTTP，免安装）

服务运行在魔搭社区创空间上，客户端只需配置远程地址（以下内容粘贴到客户端配置，
注意把令牌占位符换成你的真实值）：

```text
{
  "mcpServers": {
    "printguard": {
      "type": "streamable_http",
      "url": "https://studio-miluzzz-printguard.api-inference.modelscope.net/mcp",
      "headers": {
        "Authorization": "Bearer <你的 ModelScope 访问令牌>"
      }
    }
  }
}
```

- 需要一个 ModelScope 访问令牌（免费）：modelscope.cn → 访问令牌 页面获取，
  填入上方 `Authorization` 请求头
- 产物为签名限时下载链接；生成新模型同样在工具参数 `api_key` 中自带 AHOLO key

接入后对 Agent 说：**"帮我做一个能打印的手机支架，高 10 厘米"**。

## 两种质量档

| | `fast`（默认） | `fine` |
|---|---|---|
| 适用 | 结构件/支架 | **手办/细节件** |
| 配方 | G1-Turbo + 100k + 自适应 pitch | G1 + 300k + 0.3mm 细 pitch |
| 耗时 | ~2 分钟 | ~10 分钟 |

Agent 会按场景自动选择；交付物为 `printable.3mf` + `turntable.gif`（渲染预览；诊断图作为内部诊断件供 agent 讲解）。

## 本地开发（Linux / WSL）

```bash
uv venv --python 3.13 .venv          # 或 python3 -m venv .venv（需 3.11+）
.venv/bin/python -m pip install -e . # 或 uv pip install -e .
.venv/bin/python printguard.py selftest        # 8 个检查用例
.venv/bin/python pipeline.py "测试" --height-mm 100
.venv/bin/python mcp_server.py                 # MCP stdio（本机桌面宿主）
.venv/bin/python mcp_server.py --transport streamable-http --port 8000   # MCP HTTP（云端托管；魔搭创空间用 7860）
```

协议回归（Linux 版）：

```bash
.venv/bin/python test_mcp_client.py   # stdio 形态：instructions / enum / 错误语义
.venv/bin/python test_mcp_http.py     # HTTP 形态：MCP over HTTP + 签名下载链接
```

详见 `PrintGuard.md`（技术方案）、`docs/deploy-modelscope.md`（云端部署）与
`.dsh/skills/printguard/SKILL.md`（Agent 编排规约）。
