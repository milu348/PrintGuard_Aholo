"""Asset 上传链：把本地字节变成开放平台可访问的干净公网 URL（OUS COS 直传）。

协议（官方 OpenAPI「Asset」标签，2026-11 实测口径）：
1. `GET /asset/v1/token`（Authorization 头）→ `{ousToken, globalDomain, blockSize}`
   —— ousToken 一次一用：绑定一个上传任务，用完即废（errorCode 12 = Token 已使用）
2. 文件 ≤ blockSize：`POST {globalDomain}/ous/api/v2/single/upload`
   multipart 表单：`file`（二进制）+ `md5`（十六进制小写）；头 `ous-token-v2`
3. 文件 > blockSize：`POST /ous/api/v2/block/upload/init?md5=&blocks=&size=&name=`
   （query 传参，无 body）→ 逐片 `POST /ous/api/v2/block/upload/part`
   表单 `block`（序号从 1 开始）+ `file`；同任务分片并发建议 ≤2
4. 轮询 `GET {globalDomain}/ous/api/v2/upload/status`（只带 ous-token-v2 头）：
   `d.status` 5=成功（取 `d.url`）、6/8=失败/中止（终态报错）、其余继续轮询

平台硬规则（lux3d 集成笔记实测）：**平台签名 URL 不能直接喂给生成任务**（秒败
status=4），普通公网 URL 与 Asset URL 都可以——所以本地 / base64 图片必须走本模块
中转成 Asset URL。

服务运行约定：
- 日志只走 stderr；HTTP 会话由调用方注入（Lux3DClient 已按线程隔离）
- 所有错误统一抛 `Lux3DError`（复用 lux3d 的错误类型，调用方只捕获一种）
"""
from __future__ import annotations

import hashlib
import logging
import math
import time
import urllib.parse
from dataclasses import dataclass

import requests

from .client import Lux3DError

logger = logging.getLogger("printguard.lux3d.asset")

# 上传状态（官方枚举）：仅 5/6/8 是终态
_OUS_STATUS_SUCCESS = 5
_OUS_STATUS_FAILED = (6, 8)
_STATUS_NAMES = {0: "草稿", 1: "基础检测完成", 2: "内容检测完成", 3: "排队上传",
                 4: "上传中", 5: "上传成功", 6: "上传失败", 7: "审核中", 8: "手动中止"}

# 轮询节奏：官方建议间隔 ≥200ms、总时长 5s 量级；这里放宽兜底网络抖动
_POLL_INTERVAL_SEC = 0.5
_POLL_MAX_SEC = 60.0
_UPLOAD_TIMEOUT_SEC = 120.0


@dataclass(frozen=True)
class OusToken:
    """一次上传任务的凭证。token 与任务绑定，一次一用。"""

    token: str
    domain: str          # globalDomain，如 https://ous-cos.kujiale.com
    block_size: int


def _parse_envelope(resp, action: str) -> dict:
    """OUS 接口与开放平台同用 {c, m, d} 信封；c == "0" 才放行。"""
    try:
        payload = resp.json()
    except ValueError:
        raise Lux3DError(f"{action}: HTTP {resp.status_code} 非 JSON 响应",
                         http_status=resp.status_code) from None
    if not isinstance(payload, dict):
        raise Lux3DError(f"{action}: 响应不是 JSON 对象（{type(payload).__name__}）",
                         http_status=resp.status_code)
    if str(payload.get("c", "")) != "0":
        raise Lux3DError(f"{action} 失败 c={payload.get('c')} m={payload.get('m')}",
                         code=str(payload.get("c", "")),
                         http_status=resp.status_code, data=payload)
    return payload


def fetch_upload_token(session: requests.Session, base_url: str,
                       headers: dict) -> OusToken:
    """获取 OUS 上传凭证（每次上传都要取新的，token 一次一用）。"""
    resp = session.get(f"{base_url}/asset/v1/token", headers=headers,
                       timeout=_UPLOAD_TIMEOUT_SEC)
    d = _parse_envelope(resp, "获取上传凭证").get("d") or {}
    token = str(d.get("ousToken") or "").strip()
    domain = str(d.get("globalDomain") or "").strip().rstrip("/")
    block_size = int(d.get("blockSize") or 0)
    if not token or not domain or block_size <= 0:
        raise Lux3DError(f"上传凭证不完整：{d}", data=d)
    return OusToken(token=token, domain=domain, block_size=block_size)


def _md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def _upload_single(session: requests.Session, ous: OusToken,
                   data: bytes, filename: str) -> None:
    """单文件直传（≤ blockSize）。成功后凭证已绑定任务，等 poll 返回 URL。"""
    resp = session.post(
        f"{ous.domain}/ous/api/v2/single/upload",
        headers={"ous-token-v2": ous.token},
        files={"file": (filename, data, "application/octet-stream")},
        data={"md5": _md5(data)},
        timeout=_UPLOAD_TIMEOUT_SEC)
    _parse_envelope(resp, "上传文件")


def _upload_in_blocks(session: requests.Session, ous: OusToken,
                      data: bytes, filename: str) -> None:
    """分片直传（> blockSize）：init → 逐片 part（序号从 1 开始）。"""
    blocks = math.ceil(len(data) / ous.block_size)
    query = urllib.parse.urlencode({
        "md5": _md5(data), "blocks": blocks, "size": len(data), "name": filename})
    resp = session.post(
        f"{ous.domain}/ous/api/v2/block/upload/init?{query}",
        headers={"ous-token-v2": ous.token}, timeout=_UPLOAD_TIMEOUT_SEC)
    _parse_envelope(resp, "初始化分片上传")
    for i in range(1, blocks + 1):
        chunk = data[(i - 1) * ous.block_size: i * ous.block_size]
        resp = session.post(
            f"{ous.domain}/ous/api/v2/block/upload/part",
            headers={"ous-token-v2": ous.token},
            files={"file": (filename, chunk, "application/octet-stream")},
            data={"block": str(i)},
            timeout=_UPLOAD_TIMEOUT_SEC)
        _parse_envelope(resp, f"上传分片 {i}/{blocks}")
        logger.debug("asset 分片 %d/%d 上传完成", i, blocks)


def poll_upload_status(session: requests.Session, ous: OusToken) -> str:
    """轮询上传状态到终态，返回文件公网 URL。6/8 判失败，超时报错。"""
    deadline = time.monotonic() + _POLL_MAX_SEC
    while True:
        resp = session.get(f"{ous.domain}/ous/api/v2/upload/status",
                           headers={"ous-token-v2": ous.token},
                           timeout=_UPLOAD_TIMEOUT_SEC)
        d = _parse_envelope(resp, "查询上传状态").get("d") or {}
        status = int(d.get("status", -1))
        if status == _OUS_STATUS_SUCCESS:
            url = str(d.get("url") or "").strip()
            if not url:
                raise Lux3DError(f"上传成功但未返回 URL：{d}", data=d)
            return url
        if status in _OUS_STATUS_FAILED:
            code = d.get("errorCode")
            raise Lux3DError(
                f"上传失败（status={status} {_STATUS_NAMES.get(status, '')}"
                f"{f' errorCode={code}' if code is not None else ''}）：{d}",
                code=str(status), data=d)
        if time.monotonic() > deadline:
            raise Lux3DError(
                f"上传状态轮询超时（{_POLL_MAX_SEC:.0f}s，仍处于 "
                f"{_STATUS_NAMES.get(status, f'未知({status})')}）", data=d)
        time.sleep(_POLL_INTERVAL_SEC)


def upload_bytes(session: requests.Session, base_url: str, headers: dict,
                 data: bytes, filename: str) -> str:
    """上传一条完整链路：取凭证 →（单文件 | 分片）→ 轮询 → URL。"""
    ous = fetch_upload_token(session, base_url, headers)
    if len(data) <= ous.block_size:
        _upload_single(session, ous, data, filename)
    else:
        _upload_in_blocks(session, ous, data, filename)
    return poll_upload_status(session, ous)
