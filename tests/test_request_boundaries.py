import asyncio
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
from PIL import Image

from image_workbench import StorageConfig, StorageManager
from image_workbench.remote_access import RemoteAccess
from image_workbench.storage import MetadataError
from image_workbench.web import MAX_AUTH_BYTES, MAX_UPLOAD_BYTES, create_app


class RequestBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.storage = StorageManager(StorageConfig(Path(self.temp.name) / "data", max_image_bytes=2048))
        self.app = create_app(self.storage, backend="local")
        self.client = TestClient(self.app, base_url="http://localhost", raise_server_exceptions=False)
        self.addCleanup(self.client.close)
        self.public_csrf = self.client.get("/api/auth/config").json()["csrf_token"]
        credentials = {"username": "boundary_user", "password": "boundary-test-password"}
        headers = {"X-Workbench-CSRF": self.public_csrf}
        self.assertEqual(self.client.post("/api/auth/register", json=credentials, headers=headers).status_code, 200)
        self.assertEqual(self.client.post("/api/auth/login", json=credentials, headers=headers).status_code, 200)
        self.csrf = self.client.get("/api/config").json()["csrf_token"]
        self.client.headers["X-Workbench-CSRF"] = self.csrf
        self.info = self.client.get("/api/auth/me").json()

    def raw_request(self, path, *, method="GET", body_size=0, chunk_size=1024 * 1024,
                    declared=None, authenticated=False, app=None, host="localhost", headers=()):
        reads, count, events = 0, 0, []
        request_headers = [(b"host", host.encode()), *headers]
        if declared is not None:
            request_headers.append((b"content-length", str(declared).encode()))
        if authenticated:
            request_headers.extend([(b"cookie", ("iw_session=" + self.client.cookies.get("iw_session")).encode()),
                                    (b"x-workbench-csrf", self.csrf.encode())])

        async def receive():
            nonlocal reads, count
            reads += 1
            size = min(chunk_size, body_size - count)
            count += size
            return {"type": "http.request", "body": b"x" * size, "more_body": count < body_size}

        async def send(event):
            events.append(event)

        scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"}, "http_version": "1.1",
                 "method": method, "scheme": "https" if host != "localhost" else "http", "path": path,
                 "raw_path": path.encode(), "query_string": b"", "headers": request_headers,
                 "client": ("127.0.0.1", 1234), "server": (host, 80)}
        asyncio.run((app or self.app)(scope, receive, send))
        start = next(event for event in events if event["type"] == "http.response.start")
        return start["status"], reads, count, dict(start["headers"])

    def test_anonymous_private_requests_are_rejected_without_reading_bodies(self):
        for method, path, expected in (("GET", "/api/artifacts/" + uuid4().hex, 401),
                                       ("POST", "/api/main-projects", 401), ("GET", "/", 303)):
            with self.subTest(method=method, path=path):
                status, reads, count, headers = self.raw_request(path, method=method,
                    body_size=40 * 1024 * 1024, declared=40 * 1024 * 1024,
                    headers=[(b"x-workbench-csrf", self.public_csrf.encode())])
                self.assertEqual((status, reads, count), (expected, 0, 0))
                if path.startswith("/api/"):
                    self.assertEqual(headers[b"cache-control"], b"no-store")

    def test_public_routes_and_authenticated_reads_do_not_buffer_bodies(self):
        for path, method, authenticated in (("/api/main-projects", "GET", True), ("/login", "GET", False),
                                            ("/api/auth/config", "GET", False), ("/static/workbench.js", "HEAD", False)):
            with self.subTest(path=path, method=method):
                status, reads, count, _ = self.raw_request(path, method=method, authenticated=authenticated,
                    body_size=40 * 1024 * 1024, declared=40 * 1024 * 1024)
                self.assertEqual((status, reads, count), (200, 0, 0))

    def test_public_read_only_routes_reject_writes_without_consuming_body(self):
        for path in ("/login", "/api/auth/config", "/static/workbench.js"):
            with self.subTest(path=path):
                status, reads, count, _ = self.raw_request(path, method="POST", body_size=1024 * 1024,
                    headers=[(b"x-workbench-csrf", self.public_csrf.encode())])
                self.assertEqual((status, reads, count), (405, 0, 0))

    def test_auth_writes_keep_declared_and_actual_16kib_limit(self):
        for path in ("/api/auth/login", "/api/auth/register", "/api/auth/activate"):
            with self.subTest(path=path):
                status, reads, count, headers = self.raw_request(path, method="POST", declared=MAX_AUTH_BYTES + 1,
                    headers=[(b"x-workbench-csrf", self.public_csrf.encode())])
                self.assertEqual((status, reads, count), (413, 0, 0))
                self.assertEqual(headers[b"cache-control"], b"no-store")
                status, reads, count, _ = self.raw_request(path, method="POST", body_size=MAX_AUTH_BYTES + 1,
                    chunk_size=8192, headers=[(b"x-workbench-csrf", self.public_csrf.encode())])
                self.assertEqual((status, reads, count), (413, 3, MAX_AUTH_BYTES + 1))

    def test_authenticated_writes_keep_declared_and_streamed_upload_limit(self):
        limit = MAX_UPLOAD_BYTES + 1024 * 1024
        status, reads, count, _ = self.raw_request("/api/main-projects", method="POST", authenticated=True,
                                                 declared=limit + 1)
        self.assertEqual((status, reads, count), (413, 0, 0))
        status, reads, count, _ = self.raw_request("/api/main-projects", method="POST", authenticated=True,
                                                 body_size=limit + 1)
        self.assertEqual(status, 413)
        self.assertEqual(count, limit + 1)

    def test_worker_auth_remains_separate_and_rejects_before_body(self):
        access = RemoteAccess("https://images.example", "synthetic-worker-token-" + "r" * 32)
        remote = create_app(self.storage, backend="local", remote_access=access)
        for headers in ([], [(b"authorization", b"Bearer invalid")]):
            status, reads, count, _ = self.raw_request("/api/worker/claim", method="POST", app=remote,
                host=access.host, body_size=40 * 1024 * 1024, headers=headers)
            self.assertEqual((status, reads, count), (401, 0, 0))
        status, reads, count, _ = self.raw_request("/api/main-projects", app=remote, host=access.host,
            body_size=40 * 1024 * 1024, headers=[(b"authorization", access.worker_authorization)])
        self.assertEqual((status, reads, count), (401, 0, 0))
        service = TestClient(remote, base_url=access.origin, raise_server_exceptions=False)
        self.addCleanup(service.close)
        response = service.post("/api/worker/claim", headers={"Authorization": access.worker_authorization.decode()},
                                json={"worker_id": uuid4().hex, "backend": "local"})
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()["job"])

    def test_expected_4xx_do_not_grow_traceback_log(self):
        for _ in range(3):
            self.assertEqual(self.client.get("/api/artifacts/invalid-id").status_code, 400)
            self.assertEqual(self.client.get("/api/artifacts/" + uuid4().hex).status_code, 404)
        request_id = uuid4().hex
        self.assertEqual(self.client.post("/api/main-projects", json={"request_id": request_id, "name": "first"}).status_code, 200)
        self.assertEqual(self.client.post("/api/main-projects", json={"request_id": request_id, "name": "different"}).status_code, 409)
        self.assertFalse((self.storage.data_root / ".workbench" / "web-errors.log").exists())

    def test_unexpected_and_metadata_errors_keep_local_diagnostics_only(self):
        scoped = self.app.scoped_app(self.info["account_id"], self.info["workspace_id"])
        for error, expected in ((RuntimeError("synthetic internal failure"), 500),
                                (MetadataError("synthetic corrupt metadata"), 400)):
            with self.subTest(error=type(error).__name__):
                with patch.object(scoped.state.workbench.storage, "get_artifact", side_effect=error):
                    response = self.client.get("/api/artifacts/" + uuid4().hex)
                self.assertEqual(response.status_code, expected)
                self.assertNotIn(str(error), response.text)
                self.assertNotIn("Traceback", response.text)
                self.assertEqual(response.headers["cache-control"], "no-store")
                log = (self.storage.data_root / ".workbench" / "web-errors.log").read_text(encoding="utf-8")
                self.assertIn(str(error), log)
                self.assertIn("Traceback", log)

    def test_valid_upload_and_image_response_still_work(self):
        project = self.client.post("/api/main-projects", json={"request_id": uuid4().hex, "name": "upload"}).json()
        sid = project["subprojects"][0]["subproject_id"]
        image = io.BytesIO()
        Image.new("RGB", (16, 16), "green").save(image, "PNG")
        response = self.client.post(f"/api/subprojects/{sid}/tasks",
            data={"request_id": uuid4().hex, "operation": "generate", "prompt": "isolated fixture"},
            files={"files": ("../../outside.png", image.getvalue(), "text/html")})
        self.assertEqual(response.status_code, 200)
        picture = self.client.get(response.json()["inputs"][0]["url"])
        self.assertEqual(picture.status_code, 200)
        self.assertEqual(picture.content, image.getvalue())
        self.assertEqual(picture.headers["cache-control"], "no-store")
        self.assertEqual(picture.headers["content-type"], "image/png")
        self.assertEqual(self.app.state.workbench.list_jobs(), [])


if __name__ == "__main__":
    unittest.main()
