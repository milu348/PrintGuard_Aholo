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
- download 默认拒绝内网与保留地址（PRINTGUARD_ALLOW_PRIVATE_URLS=1 关闭）
- key 只从调用方参数 / 环境变量读取；托管环境不放本地密钥文件

能力一览（生成只做「返回模型文件」的三种，其余平台能力一概不用）：
- text_to_3d          文生 3D（风格）；传可选 img 即为「文图混合」生成
- img_to_3d           图生 3D：img 单图 或 imgs 1-32 张多视角
- get_task / wait     任务查询与轮询；list_tasks 生成记录分页
- upload_image        Asset 上传链：本地字节 → 干净公网 URL（协议见 asset.py）
- rehost              下载任意外链图片 → 转存 Asset URL（链式生成时绕开签名 URL 限制）

已删除（不返回模型文件的平台能力，产品明确不用）：
multimodal_to_image（多模态出图，返回的是图片不是模型）、image_to_four_view、
material_transfer、part_split、multi_format_export、articulation_animation、
rig/retarget，以及 download_bytes。
"""
from __future__ import annotations

import ipaddress
import json
import logging
import os
import socket
import sys
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

import requests

logger = logging.getLogger("printguard.lux3d")

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
FACE_COUNT_MIN, FACE_COUNT_MAX = 10_000, 300_000

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

    # ---------- 能力：文字 / 图文混合生成 3D ----------

    def text_to_3d(self, prompt: str, *, version: str = "G1-Turbo",
                   face_count: int = 100_000,
                   style: str | None = None,
                   img: str | None = None) -> int:
        """文生 3D（或文图混合），返回 taskid。

        style 取值见 STYLES（默认 photorealistic，打印场景用白模）。
        img：可选参考图 URL——给了就是官方的「文图混合」模式（prompt 描述要
        在参考图基础上生成/修改什么）；本地图片请先经 upload_image() 转 URL。
        平台侧 outputFormat 固定 glb、enablePbr 固定 False（白模）、
        aiPredictSize 固定 False——调用方从未用过其他值，不做参数暴露。
        """
        if not str(prompt or "").strip():
            raise ValueError("prompt 不能为空")
        body = _gen_body(version=version, face_count=face_count, prompt=prompt)
        if style:
            if style not in STYLES:
                raise ValueError(f"style 须为 {STYLES} 之一，得到 {style!r}")
            body["style"] = style
        if img:
            body["img"] = img
        return self._post_create("/lux3d/v1/generate/text-to-3d/task/create", body)

    # ---------- 能力：图生 3D ----------

    def img_to_3d(self, *, version: str, face_count: int = 100_000,
                  image: str | None = None,
                  images: list[str] | None = None) -> int:
        """图生 3D，返回 taskid。image（单图）与 images（1-32 张多视角）二选一。

        官方约束：img 与 imgs 必须且只能提供一个；imgs 保留输入顺序、第一张
        为主参考，且**必须显式传 version**（本方法的 version 因此是必填参数）。
        图片 URL 不能为空白；本地图片请先经 upload_image() 转 URL。
        本方法没有 prompt 字段——带文字描述的需求走 text_to_3d(img=...)。
        """
        if image and images:
            raise ValueError("image 与 images 只能提供一个")
        if not image and not images:
            raise ValueError("必须提供 image（单图）或 images（1-32 张多视角）")
        urls = ([image] if image else [str(u).strip() for u in (images or [])])
        urls = [u for u in urls if u]
        if not urls:
            raise ValueError("图片 URL 不能为空白")
        if images and not 1 <= len(urls) <= 32:
            raise ValueError(f"imgs 支持 1-32 张，得到 {len(urls)} 张")
        body = _gen_body(version=version, face_count=face_count)
        body["imgs" if (images and not image) else "img"] = (
            urls if (images and not image) else urls[0])
        return self._post_create("/lux3d/v1/generate/img-to-3d/task/create", body)

    # ---------- 能力：Asset 上传（图片 → URL） ----------

    def upload_image(self, data: bytes, filename: str = "image.png") -> str:
        """把本地图片字节经 Asset 上传链转成干净公网 URL。

        适用输入：MCP 客户端传 base64 / data URI、服务端本地路径、网页前端
        上传。返回的 URL 可直接用于 text_to_3d(img=...) / img_to_3d(...)。
        """
        from .asset import upload_bytes

        if not data:
            raise ValueError("图片内容为空")
        return upload_bytes(self._http, self.base_url, self._headers(),
                            data, filename)

    def rehost(self, url: str) -> str:
        """下载任意外链图片并转存为 Asset URL。

        用途：链式生成时平台签名 URL 不能作为下游输入（实测秒败 status=4），
        需要「拿一个 URL 再喂给生成任务」时用它中转。下载走 SSRF 校验。
        """
        self._assert_public_url(url)
        name = url.split("?")[0].rstrip("/").rsplit("/", 1)[-1] or "image.png"
        if "." not in name:
            name += ".png"
        resp = self._http.get(url, timeout=self.download_timeout)
        resp.raise_for_status()
        return self.upload_image(resp.content, name)

    # ---------- 下载 ----------

    def _assert_public_url(self, url: str) -> None:
        """防 SSRF：拒绝解析到内网/保留地址的下载目标。

        托管形态会对用户传入的 URL 做下载，必须挡住云元数据、本机端口等目标。
        allow_private_urls=True 可关闭（本地开发用）。
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

def _gen_body(*, version: str, face_count: int,
              prompt: str | None = None) -> dict:
    """生成类任务公共请求体（含官方约束校验）。

    平台固定项内联在此：outputFormat 只取 glb（交付格式由本地导出决定）、
    enablePbr=False（打印要白模）、aiPredictSize=False（尺寸由 height_mm 锁定）。
    三者曾是对外参数，所有调用方传同一组值，已收敛。
    """
    if version not in VERSIONS:
        raise ValueError(f"version 须为 {VERSIONS} 之一，得到 {version!r}")
    if not FACE_COUNT_MIN <= int(face_count) <= FACE_COUNT_MAX:
        raise ValueError(f"faceCount 取值 {FACE_COUNT_MIN}-{FACE_COUNT_MAX}，"
                         f"得到 {face_count}")
    body = {"version": version,
            "faceCount": int(face_count),
            "outputFormat": ["glb"],
            "enablePbr": False,
            "aiPredictSize": False}
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
