# Lux3D API 集成笔记

客户端：`printguard_aholo/lux3d/`（client.py 业务接口 + asset.py 上传链，契约逐项对照官方 OpenAPI）
测试环境：`.venv`（Python 3.10）+ 真实 API Key

## 实测结果矩阵

| 能力 | 输入 | 结果 | 耗时 |
|---|---|---|---|
| 概念图 concept | 纯文字 | ✅ | 40s |
| 四视图 four_view | 纯文字 | ✅ | 81s |
| 四视图 four_view | 平台签名 URL | ❌ 秒败 (status=4) | 15s |
| 四视图 four_view | Asset URL | ✅ | 61s |
| 图生 3D image_to_3d | 平台签名 URL | ❌ 秒败 (status=4) | 5s |
| 图生 3D image_to_3d | 普通公网 URL | ✅ | 56s |
| 图生 3D image_to_3d | Asset URL | ✅ | 55s |
| 文生 3D text_to_3d | 文字 + cartoon | ✅ | 86s |
| Asset 上传 | 本地 jpg | ✅ | — |
| 任务查询/下载 | — | ✅ | — |

## 关键发现

1. **平台生成的签名 URL 不能作为下游生成任务的输入**（两条能力均 5-15s 快速
   status=4，且任务详情无 outputs、无错误信息）。结果 URL 自己下载没问题，
   但喂给下一个生成任务会被拒。**链式生成（概念图→四视图→图生 3D）必须用
   `client.rehost(url)` 中转**：下载 → Asset 上传 → 干净 URL 作为输入。
2. 四视图结果把 4 个 URL 打包成 **JSON 数组字符串**放进单个 output，
   `wait()` 已自动展开（直接按 outputs 数量判断会误判为 1 张）。
3. 缺 key 时平台返回 **HTTP 200 + c="-1" m="appKey missing"**（不是 401），
   客户端按鉴权错误快速失败，不进入轮询重试。
4. 失败任务 `task/get` 只回 `{bizId, status, taskId}`，多数无失败原因；
   个别能力（如绑骨）会把 error JSON 放在 outputs 里，wait() 会透出。
5. 任务状态机：0 初始化 / 1 运行中 / 3 成功 / 4 失败 / 6 已取消；
   生成类并发=1，`Lux3DClient` 对 create 全局加锁 + 限流退避 30s。
6. 结果 URL 有效期 **2 小时**，拿到 `TaskResult` 应立即 `download_outputs()`。

## 怎么跑一次生成

客户端 CLI 冒烟入口已删除（v0.1.4 收尾时清掉，能力全部由 MCP 工具承接）。
要手动验证平台链路，用 MCP 工具或网页 Agent：

```bash
# 本地起服务（stdio）
.venv/bin/printguard-aholo
# 或托管形态
PRINTGUARD_TRANSPORT=streamable-http .venv/bin/python -m printguard_aholo.server
```

然后在 MCP 客户端里调 `lux3d_text_to_3d` / `lux3d_image_to_3d` /
`lux3d_get_task` 等，或直接用 `printguard_run` 走完整链路；
也可以开网页用自然语言 / 图片描述需求。
产物落在 run 目录（本地）或以限时链接交付（托管）。

> v0.2 恢复图片生成：`lux3d_image_to_3d`（单图 / 1-32 张多视角）与
> `lux3d_text_to_3d(image=...)`（文图混合）已回归产品；
> `multimodal_to_image` / `four_view` / `material_transfer` 等不返回模型文件的
> 平台能力仍然不用。本地图片经 `lux3d/asset.py`（Asset 上传链）转干净 URL 后
> 喂给生成任务。

## Asset 上传链（v0.2，官方 OpenAPI「Asset」标签）

1. `GET /asset/v1/token`（Authorization 头）→ `{ousToken, globalDomain, blockSize}`，
   token 一次一用（errorCode 12 = Token 已使用）；
2. 文件 ≤ blockSize：`POST {globalDomain}/ous/api/v2/single/upload`
   （multipart：`file` + `md5`；头 `ous-token-v2`）；
3. > blockSize：`block/upload/init?md5=&blocks=&size=&name=`（query 传参）→
   逐片 `block/upload/part`（表单 `block` 序号**从 1 开始** + `file`）；
4. 轮询 `GET /ous/api/v2/upload/status`：`d.status` 5=成功（取 `d.url`）、
   6/8=失败/中止终态，其余继续轮询。

## 离线测试

```bash
.venv/bin/python -m unittest discover -s tests -t .   # 22 用例，伪造传输层，零网络
```

## 托管部署注意（魔搭创空间 MCP 形态）

- **key 来源**：托管环境只认工具参数 `api_key` 或环境变量 `AHOLO_API_KEY`；
  不要把 `AholoAPI.json` / `secrets.local.json` 打进镜像（已在 .gitignore）。
- **stdout 纪律**：客户端日志全部走 stderr；MCP stdio 传输时 stdout 只能有协议帧。
- **线程安全**：`requests.Session` 按线程隔离；`Lux3DClient` 可被多线程共享，
  create 会自动排队（平台账号级并发=1，排队上限 `max_wait`，默认 20 分钟）。
- **SSRF**：托管形态用户可传入任意 URL（rehost 会下载），默认拒绝内网/保留地址；
  本地调试确有需要时设 `PRINTGUARD_ALLOW_PRIVATE_URLS=1`。
- **超时**：生成任务可能跑 10 分钟，`wait()` 默认 20 分钟上限，不要按普通 HTTP 请求超时配置。
