"""输入归一化：把调用方给的各种输入形式统一变成管线能直接用的东西。

三类输入：
- source（体检 / 修复用模型）：本地路径直接用；URL 经客户端下载（带 SSRF 防护）
- image（生成用参考图 / 主图）：公网 URL 直接透传（平台实测可用）；本地路径
  或 data URI（base64）先经 Asset 上传链转成干净 URL——平台签名 URL 不能
  直接喂给生成任务（秒败 status=4，见 lux3d 集成笔记），Asset URL 实测可用
- 客户端构造：make_client 是全服务唯一的 Lux3DClient 构造入口
"""
from __future__ import annotations

import base64
import binascii
import logging
import urllib.parse
from pathlib import Path

from ..lux3d import Lux3DClient
from .execute import PipelineError

logger = logging.getLogger("printguard.pipeline")

# 允许作为生成输入的图片后缀（官方 World 接口口径，img-to-3d 实测一致）
IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp")
_DATA_URI_PREFIX = "data:"


def is_url(value: str) -> bool:
    """是否 http(s) URL（本地路径直接读，URL 走客户端下载）。"""
    return str(value).startswith(("http://", "https://"))


def make_client(client: Lux3DClient | None,
                api_key: str | None,
                *, verbose: bool = False) -> Lux3DClient:
    """服务内唯一的客户端构造入口：支持注入（测试/复用），否则按 key 新建。

    verbose 由调用方从 Settings 传入，所有构造路径一致——避免 tools/pipeline
    各自 new 一个导致 mock 不到（实测坑过）。
    """
    return client or Lux3DClient(api_key, verbose=verbose)


def _decode_data_uri(value: str) -> tuple[bytes, str]:
    """解析 data URI（data:image/png;base64,xxx）→ (字节, 推断文件名)。"""
    head, _, b64 = value.partition(",")
    meta = head[5:].lower()
    ext = "png" if "png" in meta else ("webp" if "webp" in meta else "jpg")
    try:
        return base64.b64decode(b64, validate=False), f"input.{ext}"
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"data URI 解析失败：{exc}") from exc


def normalize_image_url(client: Lux3DClient, value: str) -> str:
    """单个图片输入 → 平台可直接使用的公网 URL。

    - http(s) URL：直接透传（普通公网 URL 平台可用；签名 URL 平台自会拒绝）
    - data URI：解码 → Asset 上传
    - 本地路径：读文件 → Asset 上传
    """
    value = str(value).strip()
    if not value:
        raise ValueError("图片输入不能为空")
    if is_url(value):
        return value
    if value.startswith(_DATA_URI_PREFIX):
        data, name = _decode_data_uri(value)
        url = client.upload_image(data, name)
        logger.info("图片经 Asset 中转：%s（%d B）", name, len(data))
        return url
    path = Path(value)
    if not path.is_file():
        raise PipelineError(f"图片文件不存在：{value}")
    if path.suffix.lower() not in IMAGE_SUFFIXES:
        raise PipelineError(
            f"不支持的图片格式 {path.suffix!r}（允许 {IMAGE_SUFFIXES}）：{value}")
    data = path.read_bytes()
    if not data:
        raise PipelineError(f"图片文件为空：{value}")
    url = client.upload_image(data, path.name)
    logger.info("本地图片经 Asset 中转：%s（%d B）", path.name, len(data))
    return url


def normalize_generation_images(client: Lux3DClient, *,
                                image: str | None = None,
                                images: list[str] | None = None) -> list[str]:
    """生成任务的图片输入归一化：返回 URL 列表（0/1/N 张）。

    image 与 images 是同一语义的两种写法（单张 / 多张），可任传其一；
    两者同时给出时合并去重（保持顺序）。空串项直接忽略。
    """
    raw: list[str] = []
    if image:
        raw.append(image)
    for item in (images or []):
        if item:
            raw.append(item)
    urls: list[str] = []
    seen: set[str] = set()
    for item in raw:
        url = normalize_image_url(client, item)
        if url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def resolve_source(source: str, dest_dir, client: Lux3DClient | None) -> str:
    """模型来源归一化：本地路径直接返回；URL 下载到 dest_dir 后返回路径。"""
    if not is_url(source):
        require = Path(source)
        if not require.is_file():
            raise PipelineError(f"源文件不存在：{source}")
        return str(require)
    if client is None:
        raise PipelineError(
            "URL 输入需要 API Key（下载经客户端做 SSRF 校验）；"
            "也可以先下载到本地再传 path")
    suffix = Path(urllib.parse.urlparse(source).path).suffix or ".glb"
    dest = Path(dest_dir) / f"source{suffix}"
    return client.download(source, dest)
