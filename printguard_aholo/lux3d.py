"""Lux3D 开放平台 API 客户端（AHOLO）。

契约依据官方 OpenAPI v2.0（https://labs.aholo3d.cn/api-docs），字段与枚举已逐项核对。

统一任务模型：create_* 返回 taskid → wait() 轮询至终态 → download_outputs() 落盘。
响应信封 ``{f, c, m, d}``：c == "0" 成功；"GENERATION_CONCURRENCY_LIMIT_EXCEEDED"
表示生成并发已满（平台规则：生成类任务并发 = 1，必须串行）。

平台硬规则（实测 + 官方文档沉淀）：
- 生成类任务并发 = 1：客户端对 create 全局串行（跨实例共享锁），并对并发限流退避重试
- 结果 URL 有效期 2 小时：wait() 返回后应立即 download_outputs()
- 鉴权：Authorization 头直传 ApiKey（无 Bearer 前缀）
- 任务状态：0 初始化 / 1 运行中 / 3 成功 / 4 失败 / 6 已取消

服务运行约定（托管部署）：
- 日志一律走 stderr：stdio MCP 下 stdout 是协议通道，混入日志会破坏通信
- HTTP 会话按线程隔离（requests.Session 非线程安全）
- rehost / download 默认拒绝内网与保留地址（PRINTGUARD_ALLOW_PRIVATE_URLS=1 关闭）
- key 只从调用方参数 / 环境变量读取；托管环境不放本地密钥文件

能力一览：
- text_to_3d          文生 3D（可选参考图 / 风格）
- image_to_3d         图生 3D（单图，或多视角 1-32 张）
- concept_image       概念图（multimodal-to-image）
- four_view           四视图（正 45° / 对侧 45° / 侧 / 背）
- material_transfer   模型材质重绘
- multi_format_export 多格式导出（usdz / obj_zip / fbx_zip / stl / 3mf）
- part_split          部件拆分
- get_task / wait     任务查询与轮询；list_tasks 生成记录分页
- upload_file         本地文件 → 公网 URL（Asset SDK，可选依赖）
"""
from __future__ import annotations

import ipaddress
import json
import os
import socket
import sys
import tempfile
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

import requests

DEFAULT_BASE_URL = "https://api.aholo3d.cn"  # 国际环境为 https://api.aholo.ai

KEY_FILES = ("secrets.local.json", "AholoAPI.json")  # 依次查找；均不入库
ENV_KEY = "AHOLO_API_KEY"

# 任务状态（官方枚举）
STATUS_INITIALIZING = 0
STATUS_RUNNING = 1
STATUS_SUCCEEDED = 3
STATUS_FAILED = 4
STATUS_CANCELED = 6
_STATUS_NAMES = {0: "初始化", 1: "运行中", 3: "成功", 4: "失败", 6: "已取消"}
_TERMINAL_FAILED = (STATUS_FAILED, STATUS_CANCELED)

# 生成参数约束（官方）
VERSIONS = ("G1", "G1-Turbo")
STYLES = ("photorealistic", "cartoon", "anime", "hand_painted",
          "cyberpunk", "fantasy", "glass")
FORMATS_3D = ("zip", "glb", "ply")
FACE_COUNT_MIN, FACE_COUNT_MAX = 10_000, 300_000
FORMATS_EXPORT = ("usdz", "obj_zip", "fbx_zip", "stl", "3mf")

# 重试策略
_CREATE_MAX_ATTEMPTS = 5          # create 总尝试次数
_CONCURRENCY_BACKOFF_SEC = 30.0   # 并发限流退避（平台按账号串行）
_TRANSIENT_BACKOFF_SEC = 10.0     # 网络 / 5xx / 非 JSON 退避


def _status_name(status: int) -> str:
    return _STATUS_NAMES.get(status, f"未知({status})")


class Lux3DError(RuntimeError):
    """Lux3D 平台业务 / 协议错误。code 为平台错误码（如 "-1"、"GENERATION_..."）。"""

    def __init__(self, message: str, *, code: str | None = None,
                 http_status: int | None = None, data=None):
        super().__init__(message)
        self.code = code
        self.http_status = http_status
        self.data = data


def _key_from_json(data) -> str | None:
    """从 JSON 数据里提取 key：裸字符串，或字典（优先 aholo 标签）。"""
    if isinstance(data, str):
        return data.strip() or None
    if isinstance(data, dict):
        for k, v in data.items():
            if "aholo" in str(k).lower() and str(v).strip():
                return str(v).strip()
        for v in data.values():
            if isinstance(v, str) and v.strip():
                return v.strip()
    return None


def resolve_key(explicit: str | None = None) -> str:
    """取 API Key，优先级：显式入参 > 环境变量 AHOLO_API_KEY > KEY_FILES 本地文件。

    托管部署时 key 必须由调用方经工具参数 / 环境变量传入（不落盘）；
    本地开发可写 secrets.local.json 或 AholoAPI.json：
    裸字符串 ``"ak-..."``、``{"aholo": "..."}``，兼容旧 JSON-lines 格式。
    """
    if explicit and explicit.strip():
        return explicit.strip()
    env = (os.environ.get(ENV_KEY) or "").strip()
    if env:
        return env
    for name in KEY_FILES:
        path = Path(name)
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        try:
            key = _key_from_json(json.loads(text))
        except json.JSONDecodeError:
            key = None
        if key:
            return key
        for line in text.splitlines():  # 旧格式：每行一个独立 JSON 值
            line = line.strip()
            if not line:
                continue
            try:
                key = _key_from_json(json.loads(line))
            except json.JSONDecodeError:
                continue
            if key:
                return key
    raise Lux3DError(
        f"缺少 AHOLO API Key：请传参 api_key、设置环境变量 {ENV_KEY}，"
        f"或在 {' / '.join(KEY_FILES)} 写入 key"
        "（申请：https://labs.aholo3d.cn/api-keys）")


@dataclass
class TaskResult:
    """任务终态结果。outputs 为内容列表：文件 URL / JSON 字符串 / NOT_REQUESTED。"""

    task_id: int
    status: int
    outputs: list[str] = field(default_factory=list)
    elapsed_sec: float = 0.0
    raw: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == STATUS_SUCCEEDED

    @property
    def urls(self) -> list[str]:
        """仅 http(s) 文件 URL（过滤 NOT_REQUESTED 标记与 JSON 字符串结果）。"""
        return [u for u in self.outputs if u.startswith(("http://", "https://"))]

    @property
    def first_url(self) -> str:
        return self.urls[0] if self.urls else ""


class Lux3DClient:
    """Lux3D 开放平台客户端。

    create 类方法跨实例共享全局锁（平台生成并发 = 1，排队而非报错）；
    线程安全性只到「create 串行」这一层，wait/get 可并发。
    """

    _create_gate = threading.Lock()

    def __init__(self, api_key: str | None = None, *,
                 base_url: str = DEFAULT_BASE_URL,
                 poll_interval: float = 5.0,
                 max_wait: float = 1200.0,
                 request_timeout: float = 30.0,
                 download_timeout: float = 180.0,
                 session=None,
                 verbose: bool = False,
                 allow_private_urls: bool | None = None):
        self.api_key = resolve_key(api_key)
        self.base_url = base_url.rstrip("/")
        self.poll_interval = max(0.0, poll_interval)
        self.max_wait = max(0.0, max_wait)
        self.request_timeout = request_timeout
        self.download_timeout = download_timeout
        self.verbose = verbose
        if allow_private_urls is None:
            allow_private_urls = os.environ.get(
                "PRINTGUARD_ALLOW_PRIVATE_URLS", "0") == "1"
        self.allow_private_urls = allow_private_urls
        # 注入的 session 供测试/单线程使用；默认按线程隔离（requests.Session 非线程安全）
        self._injected_session = session
        self._local = threading.local()

    @property
    def _http(self):
        if self._injected_session is not None:
            return self._injected_session
        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            self._local.session = session
        return session

    # ---------- 底层 ----------

    def _log(self, message: str) -> None:
        # 必须走 stderr：MCP stdio 传输下 stdout 是协议通道，混入日志会破坏通信
        if self.verbose:
            print(f"[lux3d] {message}", file=sys.stderr, flush=True)

    def _sleep(self, seconds: float, *, reason: str = "") -> None:
        if reason:
            self._log(f"{reason}，退避 {seconds:.0f}s")
        time.sleep(seconds)

    def _headers(self) -> dict:
        return {"Authorization": self.api_key}

    def _parse_envelope(self, resp, action: str) -> dict:
        """校验 {f, c, m, d} 信封，c == "0" 才放行。"""
        try:
            payload = resp.json()
        except ValueError:
            raise Lux3DError(f"{action}: HTTP {resp.status_code} 非 JSON 响应",
                             http_status=resp.status_code)
        if not isinstance(payload, dict):
            raise Lux3DError(f"{action}: 响应不是 JSON 对象"
                             f"（{type(payload).__name__}）",
                             http_status=resp.status_code)
        if str(payload.get("c", "")) != "0":
            raise Lux3DError(f"{action} 失败 c={payload.get('c')} m={payload.get('m')}",
                             code=str(payload.get("c", "")),
                             http_status=resp.status_code, data=payload)
        return payload

    def _post_create(self, path: str, body: dict) -> int:
        """创建任务：全局串行 + 退避重试。

        - CONCURRENCY（账号级并发=1）：排队等待，上限 max_wait（大任务可能跑 10 分钟）
        - 网络 / 5xx / 非 JSON：最多 _CREATE_MAX_ATTEMPTS 次
        """
        url = f"{self.base_url}{path}"
        last_err = "未知"
        deadline = time.monotonic() + self.max_wait
        transient_attempts = 0
        with Lux3DClient._create_gate:
            while True:
                try:
                    resp = self._http.post(url, headers=self._headers(),
                                           json=body, timeout=self.request_timeout)
                except requests.RequestException as exc:
                    last_err = f"网络异常 {type(exc).__name__}: {exc}"
                    transient_attempts += 1
                    if transient_attempts >= _CREATE_MAX_ATTEMPTS:
                        break
                    self._sleep(_TRANSIENT_BACKOFF_SEC,
                                reason=f"attempt{transient_attempts} {last_err}")
                    continue
                try:
                    payload = resp.json()
                except ValueError:
                    last_err = f"HTTP {resp.status_code} 非 JSON 响应"
                    transient_attempts += 1
                    if transient_attempts >= _CREATE_MAX_ATTEMPTS:
                        break
                    self._sleep(_TRANSIENT_BACKOFF_SEC,
                                reason=f"attempt{transient_attempts} {last_err}")
                    continue
                if not isinstance(payload, dict):
                    raise Lux3DError(f"创建任务响应不是 JSON 对象：{payload!r}",
                                     http_status=resp.status_code)
                code = str(payload.get("c", ""))
                if code == "0":
                    if payload.get("d") is None:
                        raise Lux3DError(f"创建任务成功但缺少 taskid：{payload}",
                                         data=payload)
                    return int(payload["d"])
                if "CONCURRENCY" in code:
                    last_err = f"并发限流 c={code} m={payload.get('m')}"
                    if time.monotonic() >= deadline:
                        break
                    self._sleep(_CONCURRENCY_BACKOFF_SEC, reason=last_err)
                    continue
                if resp.status_code >= 500:
                    last_err = f"服务端错误 HTTP {resp.status_code}"
                    transient_attempts += 1
                    if transient_attempts >= _CREATE_MAX_ATTEMPTS:
                        break
                    self._sleep(_TRANSIENT_BACKOFF_SEC,
                                reason=f"attempt{transient_attempts} {last_err}")
                    continue
                raise Lux3DError(f"创建任务失败 c={code} m={payload.get('m')}",
                                 code=code, http_status=resp.status_code, data=payload)
        raise Lux3DError(f"创建任务失败或排队超时：{last_err}")

    # ---------- 任务查询 ----------

    def get_task(self, task_id: int) -> dict:
        """查询单个任务（返回原始 d：taskId / status / outputs）。"""
        resp = self._http.get(f"{self.base_url}/lux3d/v1/generate/task/get",
                              headers=self._headers(),
                              params={"taskid": int(task_id)},
                              timeout=self.request_timeout)
        return self._parse_envelope(resp, f"查询任务 {task_id}").get("d") or {}

    def wait(self, task_id: int, *, on_progress=None) -> TaskResult:
        """轮询至终态（默认上限 max_wait 秒）。

        status=3 成功 → TaskResult；4 失败 / 6 已取消 → Lux3DError。
        on_progress(task_id, status, elapsed_sec) 可选，用于外部展示进度。
        401/403 直接判为鉴权失败并抛出，不做无谓轮询。
        """
        start = time.monotonic()
        last_status = None
        while True:
            elapsed = time.monotonic() - start
            try:
                data = self.get_task(task_id)
            except Lux3DError as exc:
                # 鉴权类失败（HTTP 401/403，或 HTTP 200 + "appKey missing" 业务码）
                # 重试没有意义，立即抛出
                text = str(exc).lower()
                if exc.http_status in (401, 403) or "appkey" in text \
                        or "unauthorized" in text or "forbidden" in text:
                    raise
                if elapsed > self.max_wait:
                    raise Lux3DError(f"任务 {task_id} 轮询超时（{self.max_wait:.0f}s），"
                                     f"期间查询持续失败：{exc}") from exc
                self._sleep(self.poll_interval, reason=f"查询瞬态失败 {exc}")
                continue
            status = int(data.get("status", -1))
            if status not in _STATUS_NAMES:  # 协议异常：未知状态直接报错，别空转到超时
                raise Lux3DError(f"任务 {task_id} 返回未知状态 {status}：{data}",
                                 data=data)
            if status != last_status:
                self._log(f"task={task_id} status={_status_name(status)} "
                          f"elapsed={elapsed:.0f}s")
                if on_progress:
                    on_progress(task_id, status, elapsed)
                last_status = status
            if status == STATUS_SUCCEEDED:
                return TaskResult(task_id=int(data.get("taskId", task_id)),
                                  status=status,
                                  outputs=_normalize_outputs(data),
                                  elapsed_sec=round(elapsed, 1),
                                  raw=data)
            if status in _TERMINAL_FAILED:
                message = f"任务 {task_id} {_status_name(status)}（status={status}）"
                details = [c for c in _normalize_outputs(data)
                           if not c.startswith(("http://", "https://"))]
                if details:  # 部分失败会在 outputs 里带 error JSON
                    message += "：" + "; ".join(details)[:300]
                raise Lux3DError(message, code=str(status), data=data)
            if elapsed > self.max_wait:
                raise Lux3DError(f"任务 {task_id} 轮询超时（>{self.max_wait:.0f}s，"
                                 f"仍处于 {_status_name(status)}）", data=data)
            time.sleep(self.poll_interval)

    def list_tasks(self, *, page: int = 1, page_size: int = 20,
                   status: int | None = None,
                   start_time: int | None = None,
                   end_time: int | None = None) -> dict:
        """分页查询生成记录。注意平台参数名全小写：pagesize/starttime/endtime。"""
        if int(page) < 1:
            raise ValueError(f"page 从 1 开始，得到 {page}")
        if not 1 <= int(page_size) <= 100:
            raise ValueError(f"page_size 取值 1-100，得到 {page_size}")
        params: dict = {"page": int(page), "pagesize": int(page_size)}
        if status is not None:
            params["status"] = int(status)
        if start_time is not None:
            params["starttime"] = int(start_time)
        if end_time is not None:
            params["endtime"] = int(end_time)
        resp = self._http.get(f"{self.base_url}/lux3d/v1/generate/task/list",
                              headers=self._headers(), params=params,
                              timeout=self.request_timeout)
        return self._parse_envelope(resp, "查询生成记录").get("d") or {}

    # ---------- 输入处理 ----------

    def ensure_url(self, value: str) -> str:
        """URL 直传；本地路径先经 Asset 上传转为公网 URL。

        实测注意：Lux3D 结果里的签名 URL（cos...?sign=...）**不能**直接作为
        下游生成任务的输入（平台服务端拉取失败，任务快速 status=4）；
        链式生成（概念图→四视图→图生 3D）请先 rehost() 中转。
        """
        text = str(value).strip()
        if text.startswith(("http://", "https://")):
            return text
        return self.upload_file(text)

    def rehost(self, url: str) -> str:
        """外部 / 平台签名 URL → 下载 → Asset 上传 → 返回可作输入的 Asset URL。"""
        with tempfile.TemporaryDirectory(prefix="lux3d_rehost_") as td:
            local = self.download(url, Path(td))
            return self.upload_file(local)

    def upload_file(self, path: str) -> str:
        """本地文件 → 公网 URL（官方 Asset SDK，内置分块上传）。

        可选依赖：pip install manycore-aholo-sdk-asset（或项目 extra ``upload``）。
        """
        local = Path(path)
        if not local.is_file():
            raise Lux3DError(f"待上传文件不存在：{path}")
        try:
            from manycore.aholo_sdk_asset import create_asset_client
            from manycore.aholo_sdk_core import AholoClientConfig
        except ImportError as exc:
            raise Lux3DError(
                "本地文件上传需要 aholo Asset SDK："
                "pip install manycore-aholo-sdk-asset（或直接传公网 URL）") from exc
        region = "cn" if "aholo3d.cn" in self.base_url else "com"
        config = AholoClientConfig(api_key=self.api_key, region=region)
        result = create_asset_client(config).upload_file(str(local))
        return result.url

    # ---------- 能力：生成 3D ----------

    def text_to_3d(self, prompt: str, *, version: str = "G1-Turbo",
                   face_count: int = 100_000, style: str | None = None,
                   reference_image: str | None = None,
                   output_format=("glb",), enable_pbr: bool = False,
                   ai_predict_size: bool = False) -> int:
        """文生 3D，返回 taskid。

        style 取值见 STYLES（默认 photorealistic）；enablePbr 仅对 G1-Turbo 生效，
        打印场景传 False 生成白模（默认）。
        """
        body = _gen_body(prompt=prompt, version=version, face_count=face_count,
                         output_format=output_format, enable_pbr=enable_pbr,
                         ai_predict_size=ai_predict_size)
        if style:
            if style not in STYLES:
                raise ValueError(f"style 须为 {STYLES} 之一，得到 {style!r}")
            body["style"] = style
        if reference_image:
            body["img"] = self.ensure_url(reference_image)
        return self._post_create("/lux3d/v1/generate/text-to-3d/task/create", body)

    def image_to_3d(self, *, image: str | None = None,
                    images: list[str] | None = None,
                    version: str = "G1", face_count: int = 100_000,
                    output_format=("glb",), enable_pbr: bool = False,
                    ai_predict_size: bool = False) -> int:
        """图生 3D，返回 taskid。

        image（单图）与 images（1-32 张多视角，首张为主参考）必须且只能提供一个；
        官方推荐：单图用 G1，多视角用 G1-Turbo。
        """
        if bool(image) == bool(images):
            raise ValueError("image 与 images 必须且只能提供一个")
        if isinstance(images, str):
            raise ValueError("images 需为 URL/路径列表，单图请用 image 参数")
        body = _gen_body(version=version, face_count=face_count,
                         output_format=output_format, enable_pbr=enable_pbr,
                         ai_predict_size=ai_predict_size)
        if image:
            body["img"] = self.ensure_url(image)
        else:
            if not 1 <= len(images) <= 32:
                raise ValueError(f"images 数量须在 1-32，得到 {len(images)}")
            body["imgs"] = [self.ensure_url(u) for u in images]
        return self._post_create("/lux3d/v1/generate/img-to-3d/task/create", body)

    # ---------- 能力：生成图片 ----------

    def concept_image(self, prompt: str | None = None, *,
                      reference_image: str | None = None) -> int:
        """概念图（multimodal-to-image）：纯文案 / 纯图 / 图文组合，返回 taskid。"""
        if not prompt and not reference_image:
            raise ValueError("prompt 与 reference_image 至少提供一个")
        body: dict = {}
        if prompt:
            body["prompt"] = prompt
        if reference_image:
            body["img"] = self.ensure_url(reference_image)
        return self._post_create(
            "/lux3d/v1/generate/multimodal-to-image/task/create", body)

    def four_view(self, *, prompt: str | None = None,
                  image: str | None = None) -> int:
        """四视图：FRONT_THREE_QUARTER / OPPOSITE_THREE_QUARTER / SIDE / BACK。

        平台把 4 个图片 URL 打包成 JSON 数组字符串放进单个 output；
        wait() 已自动展开为 4 个 URL。
        """
        if not prompt and not image:
            raise ValueError("prompt 与 image 至少提供一个")
        body: dict = {}
        if prompt:
            body["prompt"] = prompt
        if image:
            body["img"] = self.ensure_url(image)
        return self._post_create(
            "/lux3d/v1/generate/image-to-four-view/task/create", body)

    # ---------- 能力：模型后处理 ----------

    def material_transfer(self, reference_image: str, mesh_url: str, *,
                          version: str = "v3.0-standard",
                          output_format=("zip", "glb")) -> int:
        """材质重绘：材质参考图 + GLB 模型 → 重绘后的模型。version 固定 v3.0-standard。"""
        if version != "v3.0-standard":
            raise ValueError(f"material_transfer 当前仅支持 v3.0-standard，得到 {version!r}")
        body = {"img": self.ensure_url(reference_image),
                "meshUrl": self.ensure_url(mesh_url),
                "version": version,
                "outputFormat": _check_formats(output_format,
                                               ("zip", "glb", "usdz", "obj_zip", "fbx_zip"))}
        return self._post_create("/lux3d/v1/generate/material-transfer/task/create", body)

    def multi_format_export(self, model_url: str, *,
                            output_format=("3mf",)) -> int:
        """多格式导出：zip/glb 输入 → usdz / obj_zip / fbx_zip / stl / 3mf。

        glb 输入至少选择 1 个目标格式（平台规则）。
        """
        formats = _check_formats(output_format, FORMATS_EXPORT)
        if not formats:
            raise ValueError("glb 输入至少需要 1 个导出格式")
        body = {"modelUrl": self.ensure_url(model_url), "outputFormat": formats}
        return self._post_create("/lux3d/v1/multi-format-export/task/create", body)

    def part_split(self, glb_url: str) -> int:
        """部件拆分：GLB → 含拆分结果的 GLB。同类任务进行中平台会拒绝创建。"""
        body = {"glbUrl": self.ensure_url(glb_url)}
        return self._post_create("/lux3d/v1/part-split/task/create", body)

    # ---------- 下载 ----------

    def _assert_public_url(self, url: str) -> None:
        """防 SSRF：拒绝解析到内网/保留地址的下载目标。

        托管形态会对用户传入的 URL 做下载（rehost），必须挡住云元数据、
        本机端口等目标。allow_private_urls=True 可关闭（本地开发用）。
        注意：未覆盖 DNS rebinding 场景，属基础防护。
        """
        if self.allow_private_urls:
            return
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("http", "https"):
            raise Lux3DError(f"只允许 http(s) 下载地址：{url}")
        host = parsed.hostname or ""
        try:
            infos = socket.getaddrinfo(host, None)
        except OSError:
            return  # DNS 失败交给请求层报错，避免误判
        for info in infos:
            ip = ipaddress.ip_address(info[4][0])
            if (ip.is_private or ip.is_loopback or ip.is_link_local
                    or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
                raise Lux3DError(
                    f"下载地址解析到内网/保留地址（{ip}），已拒绝：{url}")

    def download(self, url: str, dest) -> str:
        """下载单个 URL。dest 为目录时从 URL 推断文件名；返回实际文件路径。"""
        self._assert_public_url(url)
        dest = Path(dest)
        if dest.is_dir():
            name = url.split("?")[0].rstrip("/").rsplit("/", 1)[-1] or "download.bin"
            dest = dest / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        resp = self._http.get(url, stream=True, timeout=self.download_timeout)
        try:
            resp.raise_for_status()
            with open(dest, "wb") as fh:
                for chunk in resp.iter_content(1 << 16):
                    if chunk:
                        fh.write(chunk)
        finally:
            resp.close()
        return str(dest)

    def download_bytes(self, url: str) -> tuple[bytes, str]:
        """下载 URL 的内容为内存字节（一次性交付中转用），返回 (data, 文件名)。

        与 download() 共用 SSRF 校验；不落盘。
        """
        self._assert_public_url(url)
        resp = self._http.get(url, stream=True, timeout=self.download_timeout)
        try:
            resp.raise_for_status()
            chunks = [chunk for chunk in resp.iter_content(1 << 16) if chunk]
        finally:
            resp.close()
        name = url.split("?")[0].rstrip("/").rsplit("/", 1)[-1] or "image.bin"
        return b"".join(chunks), name

    def download_outputs(self, result: TaskResult, dest_dir, *,
                         extensions=None, prefix: str | None = None) -> list[str]:
        """把结果里的文件 URL 下载到目录，返回本地路径列表。

        extensions：小写扩展名集合（如 {"glb", "zip"}），None 不过滤。
        JSON 字符串结果与 NOT_REQUESTED 自动跳过。结果 URL 有效期 2 小时，拿到就下。
        """
        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        wanted = {str(e).lower().lstrip(".") for e in extensions} if extensions else None
        saved: list[str] = []
        for url in result.urls:
            last = url.split("?")[0].rstrip("/").rsplit("/", 1)[-1]
            ext = last.rsplit(".", 1)[-1].lower() if "." in last else "bin"
            if wanted and ext not in wanted:
                continue
            name = f"{prefix or result.task_id}_{len(saved)}.{ext}"
            saved.append(self.download(url, dest_dir / name))
        return saved


# ---------- 模块级工具 ----------

def _check_formats(formats, allowed) -> list[str]:
    if isinstance(formats, str):
        raise ValueError(f"output_format 需为序列（如 ('glb',)），"
                         f"不要直接传字符串 {formats!r}")
    items = [str(f) for f in (formats or [])]
    bad = [f for f in items if f not in allowed]
    if bad:
        raise ValueError(f"outputFormat 含不支持项 {bad}，可选：{allowed}")
    return items


def _gen_body(*, prompt=None, version: str, face_count: int,
              output_format, enable_pbr: bool, ai_predict_size: bool) -> dict:
    """text/img-to-3d 公共请求体（含官方约束校验）。"""
    if version not in VERSIONS:
        raise ValueError(f"version 须为 {VERSIONS} 之一，得到 {version!r}")
    if not FACE_COUNT_MIN <= int(face_count) <= FACE_COUNT_MAX:
        raise ValueError(f"faceCount 取值 {FACE_COUNT_MIN}-{FACE_COUNT_MAX}，"
                         f"得到 {face_count}")
    body = {"version": version,
            "faceCount": int(face_count),
            "outputFormat": _check_formats(output_format, FORMATS_3D),
            "enablePbr": bool(enable_pbr),
            "aiPredictSize": bool(ai_predict_size)}
    if prompt is not None:
        body["prompt"] = prompt
    return body


def _normalize_outputs(data: dict) -> list[str]:
    """提取 outputs 内容；四视图等接口把多个 URL 打包成 JSON 数组字符串，自动展开。"""
    raw = data.get("outputs") or []
    contents = [str((o or {}).get("content") or "") for o in raw]
    contents = [c for c in contents if c]
    if len(contents) == 1 and contents[0].startswith("["):
        try:
            parsed = json.loads(contents[0])
        except json.JSONDecodeError:
            return contents
        if isinstance(parsed, list) and parsed and all(isinstance(x, str) for x in parsed):
            return [x for x in parsed if x]
    return contents


# ---------- 命令行冒烟入口 ----------

def _cli(argv=None) -> int:
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        prog="lux3d", description="Lux3D 开放平台冒烟客户端（全局参数置于子命令前）")
    parser.add_argument("--key", default=None, help="API Key（缺省走环境变量/密钥文件）")
    parser.add_argument("--out", default="output/lux3d_smoke", help="下载目录")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("text3d", help="文生 3D")
    p.add_argument("prompt")
    p.add_argument("--version", default="G1-Turbo", choices=VERSIONS)
    p.add_argument("--faces", type=int, default=100_000)
    p.add_argument("--style", default=None, choices=STYLES)
    p.add_argument("--ref", default=None, help="参考图 URL 或本地路径")
    p.add_argument("--fmt", nargs="*", default=["glb"], choices=FORMATS_3D)

    p = sub.add_parser("img3d", help="图生 3D")
    p.add_argument("--image", default=None, help="单图 URL 或本地路径")
    p.add_argument("--images", nargs="+", default=None, help="1-32 张多视角（互斥于 --image）")
    p.add_argument("--version", default="G1", choices=VERSIONS)
    p.add_argument("--faces", type=int, default=100_000)
    p.add_argument("--fmt", nargs="*", default=["glb"], choices=FORMATS_3D)

    p = sub.add_parser("concept", help="概念图")
    p.add_argument("prompt", nargs="?", default=None)
    p.add_argument("--ref", default=None, help="参考图 URL 或本地路径")

    p = sub.add_parser("fourview", help="四视图")
    p.add_argument("--prompt", default=None)
    p.add_argument("--image", default=None)

    p = sub.add_parser("status", help="查询任务状态")
    p.add_argument("task_id", type=int)

    p = sub.add_parser("rehost", help="外部/签名 URL → Asset URL 中转")
    p.add_argument("url")

    p = sub.add_parser("list", help="分页查询生成记录")
    p.add_argument("--page", type=int, default=1)
    p.add_argument("--size", type=int, default=20)
    p.add_argument("--status", type=int, default=None)

    args = parser.parse_args(argv)

    try:
        client = Lux3DClient(args.key, verbose=True)
        if args.cmd == "status":
            print(json.dumps(client.get_task(args.task_id),
                             ensure_ascii=False, indent=2))
            return 0
        if args.cmd == "rehost":
            print(client.rehost(args.url))
            return 0
        if args.cmd == "list":
            print(json.dumps(client.list_tasks(page=args.page, page_size=args.size,
                                               status=args.status),
                             ensure_ascii=False, indent=2))
            return 0
        if args.cmd == "text3d":
            task_id = client.text_to_3d(args.prompt, version=args.version,
                                        face_count=args.faces, style=args.style,
                                        reference_image=args.ref,
                                        output_format=args.fmt)
        elif args.cmd == "img3d":
            task_id = client.image_to_3d(image=args.image, images=args.images,
                                         version=args.version,
                                         face_count=args.faces,
                                         output_format=args.fmt)
        elif args.cmd == "concept":
            task_id = client.concept_image(args.prompt, reference_image=args.ref)
        else:  # fourview
            task_id = client.four_view(prompt=args.prompt, image=args.image)
        result = client.wait(task_id)
        print(f"outputs ({len(result.urls)} 个 URL)：")
        for url in result.urls:
            print(f"  {url}")
        exts = {"png", "jpg", "jpeg", "webp"} if args.cmd in ("concept", "fourview") \
            else set(args.fmt)
        paths = client.download_outputs(result, args.out, extensions=exts)
        print(f"已下载 {len(paths)} 个文件 → {args.out}")
        for path in paths:
            print(f"  {path}")
        return 0
    except (Lux3DError, ValueError) as exc:
        print(f"[lux3d] 错误：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(_cli())
