# Lux3D API 集成笔记（2026-11-06 真机冒烟）

客户端：`printguard_aholo/lux3d.py`（契约逐项对照官方 OpenAPI v2.0）
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

## CLI 冒烟入口

```bash
.venv/bin/python -m printguard_aholo.lux3d concept "提示词"
.venv/bin/python -m printguard_aholo.lux3d fourview --prompt "提示词"
.venv/bin/python -m printguard_aholo.lux3d text3d "提示词" --version G1-Turbo --faces 100000 --style cartoon
.venv/bin/python -m printguard_aholo.lux3d img3d --image <URL或本地路径> --version G1-Turbo
.venv/bin/python -m printguard_aholo.lux3d status <taskid>
.venv/bin/python -m printguard_aholo.lux3d list
```

产物默认落在 `output/lux3d_smoke/`（已 gitignore）。

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
