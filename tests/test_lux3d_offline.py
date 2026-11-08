"""Lux3D 客户端离线单测：伪造传输层，不打真实 API。

运行：python -m unittest discover -s tests -t . -v
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from printguard_aholo import lux3d
from printguard_aholo.lux3d import Lux3DClient, Lux3DError, TaskResult, resolve_key
# 内部常量直接从 client 模块取/patch：包层的 re-export 是副本，
# patch 副本不影响 resolve_key 的实际读取（此处踩过）
from printguard_aholo.lux3d.client import ENV_KEY, KEY_FILES


class FakeResponse:
    def __init__(self, payload=None, status_code=200, content=b""):
        self.status_code = status_code
        self._payload = payload
        self.content = content
        self.closed = False

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size):
        yield b"fake-bytes"

    def close(self):
        self.closed = True


class FakeSession:
    """按脚本顺序返回响应，并记录调用参数供断言。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def _next(self, method, url, kw):
        self.calls.append({"method": method, "url": url, **kw})
        if not self.script:
            raise AssertionError("脚本响应耗尽")
        return self.script.pop(0)

    def post(self, url, **kw):
        return self._next("POST", url, kw)

    def get(self, url, **kw):
        return self._next("GET", url, kw)


def make_client(script):
    session = FakeSession(script)
    client = Lux3DClient(api_key="test-key", session=session,
                         poll_interval=0.0, allow_private_urls=True)
    client._sleep = lambda *a, **k: None  # 重试退避不等待真实时间
    return client, session


def envelope(d, code="0", m=""):
    return FakeResponse({"f": None, "c": code, "m": m, "d": d})


class CreateTests(unittest.TestCase):
    def test_text3d_success_and_body(self):
        client, session = make_client([envelope(42)])
        task_id = client.text_to_3d("一把椅子", version="G1-Turbo",
                                    face_count=100_000, style="cartoon")
        self.assertEqual(task_id, 42)
        call = session.calls[0]
        self.assertIn("text-to-3d", call["url"])
        self.assertEqual(call["headers"]["Authorization"], "test-key")
        body = call["json"]
        self.assertEqual(body["version"], "G1-Turbo")
        self.assertEqual(body["faceCount"], 100_000)
        self.assertEqual(body["outputFormat"], ["glb"])
        self.assertEqual(body["style"], "cartoon")
        self.assertFalse(body["enablePbr"])

    def test_param_error_no_retry(self):
        client, session = make_client([envelope(None, code="-1", m="bad request")])
        with self.assertRaises(Lux3DError) as ctx:
            client.text_to_3d("一盏灯")
        self.assertEqual(ctx.exception.code, "-1")
        self.assertEqual(len(session.calls), 1)  # 参数错误不重试

    def test_face_count_range(self):
        client, _ = make_client([])
        with self.assertRaises(ValueError):
            client.text_to_3d("x", face_count=999_999)

    def test_bad_style_rejected(self):
        client, _ = make_client([])
        with self.assertRaises(ValueError):
            client.text_to_3d("x", style="no-such-style")


class WaitTests(unittest.TestCase):
    def test_wait_success_and_progress(self):
        client, session = make_client([
            envelope({"taskId": 9, "status": 1, "outputs": []}),
            envelope({"taskId": 9, "status": 3,
                      "outputs": [{"content": "https://x/a.glb"},
                                  {"content": "NOT_REQUESTED"}]}),
        ])
        seen = []
        result = client.wait(9, on_progress=lambda tid, st, el: seen.append(st))
        self.assertTrue(result.ok)
        self.assertEqual(result.urls, ["https://x/a.glb"])  # NOT_REQUESTED 已滤除
        self.assertEqual(seen, [1, 3])
        self.assertEqual(result.elapsed_sec >= 0, True)

    def test_four_view_json_array_expansion(self):
        urls = ["https://x/1.png", "https://x/2.png",
                "https://x/3.png", "https://x/4.png"]
        client, _ = make_client([
            envelope({"taskId": 5, "status": 3,
                      "outputs": [{"content": json.dumps(urls)}]}),
        ])
        result = client.wait(5)
        self.assertEqual(result.outputs, urls)  # JSON 数组字符串已展开

    def test_wait_failure_raises(self):
        client, _ = make_client([
            envelope({"taskId": 3, "status": 4, "outputs": []}),
        ])
        with self.assertRaises(Lux3DError):
            client.wait(3)

    def test_wait_unknown_status_raises(self):
        client, _ = make_client([
            envelope({"taskId": 1, "status": 99, "outputs": []}),
        ])
        with self.assertRaises(Lux3DError) as ctx:
            client.wait(1)
        self.assertIn("未知状态", str(ctx.exception))

    def test_wait_auth_error_fails_fast(self):
        # HTTP 401：网关层鉴权失败
        client401, session401 = make_client([])
        session401.script = [FakeResponse({"f": None, "c": "x", "m": "unauthorized"},
                                          status_code=401)]
        with self.assertRaises(Lux3DError):
            client401.wait(1)
        self.assertEqual(len(session401.calls), 1)

        # HTTP 200 + 业务码 -1 + m="appKey missing"：平台实际行为（实测），
        # 同样必须快速失败而不是轮询到超时
        client_key, session_key = make_client([
            envelope(None, code="-1", m="appKey missing"),
        ])
        with self.assertRaises(Lux3DError):
            client_key.wait(1)
        self.assertEqual(len(session_key.calls), 1)


class KeyTests(unittest.TestCase):
    def test_explicit_wins(self):
        self.assertEqual(resolve_key("  ak-1  "), "ak-1")

    def test_env_var(self):
        with mock.patch.dict(os.environ, {ENV_KEY: "ak-env"}):
            with mock.patch.object(lux3d.client, "KEY_FILES", ("definitely-missing.json",)):
                self.assertEqual(resolve_key(None), "ak-env")

    def test_missing_raises(self):
        with mock.patch.dict(os.environ, {ENV_KEY: ""}):
            with mock.patch.object(lux3d.client, "KEY_FILES", ("definitely-missing.json",)):
                with self.assertRaises(Lux3DError):
                    resolve_key(None)

    def test_key_file_bare_string(self):
        with tempfile.TemporaryDirectory() as td:
            key_file = Path(td) / "secrets.json"
            key_file.write_text('"ak-bare"', encoding="utf-8")
            with mock.patch.object(lux3d.client, "KEY_FILES", (str(key_file),)):
                self.assertEqual(resolve_key(None), "ak-bare")


class ListTests(unittest.TestCase):
    def test_lowercase_query_params(self):
        client, session = make_client([
            envelope({"items": [], "total": 0, "page": 2, "pageSize": 50}),
        ])
        client.list_tasks(page=2, page_size=50, status=3,
                          start_time=1000, end_time=2000)
        params = session.calls[0]["params"]
        self.assertEqual(params["page"], 2)
        self.assertEqual(params["pagesize"], 50)   # 平台参数名全小写
        self.assertEqual(params["status"], 3)
        self.assertEqual(params["starttime"], 1000)
        self.assertEqual(params["endtime"], 2000)


class VerboseLogTests(unittest.TestCase):
    """PRINTGUARD_VERBOSE=1 时 _log 不能崩（曾因缺 import sys 而 NameError）。"""

    def test_log_writes_to_stderr(self):
        import contextlib, io
        client = Lux3DClient(session=object()) if False else None
        from printguard_aholo.lux3d import Lux3DClient as C
        c = C.__new__(C)          # 绕过 __init__（不需要真 key）
        c.verbose = True
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            c._log("冒烟")
        self.assertIn("[lux3d] 冒烟", err.getvalue())


class DownloadTests(unittest.TestCase):
    def test_download_outputs_filter_and_naming(self):
        result = TaskResult(task_id=77, status=3,
                            outputs=["https://cdn/x/model.glb?sig=1",
                                     "https://cdn/x/notes.json"],
                            elapsed_sec=1.0)
        client, _ = make_client([FakeResponse(), FakeResponse()])
        with tempfile.TemporaryDirectory() as td:
            paths = client.download_outputs(result, td,
                                            extensions={"glb"}, prefix="job")
            self.assertEqual(len(paths), 1)  # json 被 extensions 过滤
            self.assertEqual(Path(paths[0]).name, "job_0.glb")
            self.assertEqual(Path(paths[0]).read_bytes(), b"fake-bytes")

    def test_download_blocks_private_address(self):
        client = Lux3DClient(api_key="k", session=FakeSession([]),
                             allow_private_urls=False)
        with self.assertRaises(Lux3DError) as ctx:
            client.download("http://127.0.0.1/secret", "x.bin")
        self.assertIn("内网", str(ctx.exception))
        # 云元数据地址同样必须拒绝
        with self.assertRaises(Lux3DError):
            client.download("http://169.254.169.254/latest/meta-data/", "x.bin")

    def test_failure_detail_from_outputs(self):
        client, _ = make_client([
            envelope({"taskId": 3, "status": 4,
                      "outputs": [{"content": json.dumps(
                          {"error": {"code": "BAD_INPUT", "message": "fetch failed"}})}]}),
        ])
        with self.assertRaises(Lux3DError) as ctx:
            client.wait(3)
        self.assertIn("fetch failed", str(ctx.exception))  # 失败详情透出


class AssetUploadTests(unittest.TestCase):
    """Asset 上传链协议：单文件直传 / 分片（序号从 1）/ 状态轮询 / 失败终态。"""

    def _client(self, script):
        session = FakeSession(script)
        return (Lux3DClient(api_key="test-key", session=session,
                            allow_private_urls=True), session)

    def test_single_upload_returns_url(self):
        client, session = self._client([
            envelope({"ousToken": "ous-1", "globalDomain": "https://cos.example",
                      "blockSize": 1024}),
            envelope({"taskId": "u1"}),
            envelope({"status": 5, "url": "https://cos.example/u1.png"}),
        ])
        url = client.upload_image(b"img-bytes", "img.png")
        self.assertEqual(url, "https://cos.example/u1.png")
        token_req, upload, status = session.calls
        self.assertEqual(token_req["url"], "https://api.aholo3d.cn/asset/v1/token")
        self.assertEqual(upload["url"], "https://cos.example/ous/api/v2/single/upload")
        self.assertEqual(upload["headers"], {"ous-token-v2": "ous-1"})
        self.assertEqual(upload["data"], {"md5": mock.ANY})
        self.assertEqual(status["headers"], {"ous-token-v2": "ous-1"})

    def test_block_upload_part_numbers_start_at_one(self):
        data = b"x" * 1500  # blockSize=1024 → 2 片
        client, session = self._client([
            envelope({"ousToken": "ous-2", "globalDomain": "https://cos.example",
                      "blockSize": 1024}),
            envelope({"taskId": "b1", "blocks": 2}),
            envelope({"taskId": "b1"}),
            envelope({"taskId": "b1"}),
            envelope({"status": 4}),
            envelope({"status": 5, "url": "https://cos.example/b1.png"}),
        ])
        url = client.upload_image(data, "big.png")
        self.assertEqual(url, "https://cos.example/b1.png")
        init, part1, part2, poll1, poll2 = session.calls[1:]
        self.assertIn("blocks=2", init["url"])
        self.assertIn("size=1500", init["url"])
        self.assertEqual(part1["data"]["block"], "1")
        self.assertEqual(part2["data"]["block"], "2")
        self.assertEqual(poll1["url"], "https://cos.example/ous/api/v2/upload/status")

    def test_failed_status_is_terminal_error(self):
        client, _ = self._client([
            envelope({"ousToken": "t", "globalDomain": "https://cos.example",
                      "blockSize": 1024}),
            envelope({"taskId": "u"}),
            envelope({"status": 6, "errorCode": 5}),
        ])
        with self.assertRaises(Lux3DError) as ctx:
            client.upload_image(b"img", "img.png")
        self.assertIn("errorCode=5", str(ctx.exception))

    def test_rehost_downloads_then_uploads(self):
        client, session = self._client([
            FakeResponse(status_code=200, content=b"raw-image"),
            envelope({"ousToken": "t", "globalDomain": "https://cos.example",
                      "blockSize": 1024}),
            envelope({"taskId": "u"}),
            envelope({"status": 5, "url": "https://cos.example/u.png"}),
        ])
        url = client.rehost("https://cdn.example/pic.png")
        self.assertEqual(url, "https://cos.example/u.png")
        self.assertEqual(session.calls[0]["url"], "https://cdn.example/pic.png")

    def test_empty_image_rejected(self):
        client, _ = self._client([])
        with self.assertRaises(ValueError):
            client.upload_image(b"", "img.png")


if __name__ == "__main__":
    unittest.main()
