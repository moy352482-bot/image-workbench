"""Read-only list reuse: fresh next request, live permissions and path checks."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient

from image_workbench import StorageConfig, StorageManager
from image_workbench.accounts import digest
from image_workbench.project_manager import ProjectManager
from image_workbench.storage import MetadataError, _metadata_reads, metadata_read_snapshot
from image_workbench.web import create_app
from image_workbench.workspace_scope import WorkspaceProjects, WorkspaceStorage, workspace_request


class RequestMetadataCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.storage = StorageManager(StorageConfig(data_root=self.root / "data"))
        self.project = self.storage.create_project("original")
        self.task = self.storage.create_task(self.project.project_id, "generate", "original prompt")

    def get_task(self):
        return self.storage.get_task(self.project.project_id, self.task.task_id)

    def update_task_file(self, prompt):
        path = self.storage._task_dir(self.project.project_id, self.task.task_id) / "task.json"
        value = self.storage.files.read_json(path)
        value["prompt"] = prompt
        self.storage.files.write_json(path, value)

    def account_fixture(self):
        storage = StorageManager(StorageConfig(data_root=self.root / "http-data"))
        app = create_app(storage, backend="local")
        store = app.identity
        with store.connect() as db:
            aid, wid = store._account(db, "cache_owner", "unused")
            peer, pwid = store._account(db, "cache_peer", "unused")
            for token, user, space in (("owner-session", aid, wid), ("peer-session", peer, pwid)):
                db.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?)", (digest(token), user, "csrf", space, "2026-01-01T00:00:00+00:00", time.time() + 3600, "test"))
        manager = ProjectManager(storage)
        main = manager.create_main_project("account project")
        store.assign_project(main.main_project_id, wid)
        sub = manager.default_subproject(main.main_project_id)
        task = manager.create_task(sub.subproject_id, "generate", "account task")
        return app, storage, store, manager, aid, wid, peer, pwid, main, task

    def test_reuses_only_successful_immutable_records_and_clears_at_exit(self):
        with patch.object(self.storage.files, "read_json", wraps=self.storage.files.read_json) as read:
            with metadata_read_snapshot():
                first = self.get_task()
                read_count = read.call_count
                second = self.get_task()
                self.assertIs(first, second)
                self.assertEqual(read.call_count, read_count)
                with self.assertRaises(FrozenInstanceError):
                    first.prompt = "changed"
                records = _metadata_reads.get()
                self.assertTrue(records)
            self.assertEqual(records, {})
            self.assertIsNone(_metadata_reads.get())
            self.get_task()
            self.assertGreater(read.call_count, read_count)

    def test_new_request_sees_changed_and_new_records(self):
        with metadata_read_snapshot():
            original = self.get_task()
            self.update_task_file("external update")
            self.assertEqual(self.get_task(), original)
        with metadata_read_snapshot():
            self.assertEqual(self.get_task().prompt, "external update")
            fresh_id = uuid4().hex
            with self.assertRaises(FileNotFoundError):
                self.storage.get_task(self.project.project_id, fresh_id)
            fresh = self.storage.create_task(self.project.project_id, "generate", "new", task_id=fresh_id)
            self.assertEqual(self.storage.get_task(self.project.project_id, fresh_id), fresh)

    def test_exception_releases_cache_and_next_request_rejects_corruption(self):
        with self.assertRaises(RuntimeError):
            with metadata_read_snapshot():
                self.get_task()
                raise RuntimeError("interrupted response")
        self.assertIsNone(_metadata_reads.get())
        path = self.storage._task_dir(self.project.project_id, self.task.task_id) / "task.json"
        path.write_text("invalid json", encoding="utf-8")
        with metadata_read_snapshot(), self.assertRaises(MetadataError):
            self.get_task()

    def test_same_ids_in_other_storage_and_nested_context_do_not_share_records(self):
        other = StorageManager(StorageConfig(data_root=self.root / "other"))
        other.create_project("other", project_id=self.project.project_id)
        other.create_task(self.project.project_id, "generate", "other prompt", task_id=self.task.task_id)
        with metadata_read_snapshot():
            self.assertEqual(self.get_task().prompt, "original prompt")
            self.assertEqual(other.get_task(self.project.project_id, self.task.task_id).prompt, "other prompt")
            self.update_task_file("new prompt")
            with metadata_read_snapshot():
                self.assertEqual(self.get_task().prompt, "new prompt")
            self.assertEqual(self.get_task().prompt, "original prompt")
        self.assertEqual(self.get_task().prompt, "new prompt")

    def test_parallel_contexts_keep_independent_snapshots(self):
        ready, updated = threading.Event(), threading.Event()
        def first():
            with metadata_read_snapshot():
                self.assertEqual(self.get_task().prompt, "original prompt")
                ready.set()
                self.assertTrue(updated.wait(5))
                return self.get_task().prompt
        def second():
            self.assertTrue(ready.wait(5))
            self.update_task_file("parallel update")
            try:
                with metadata_read_snapshot():
                    return self.get_task().prompt
            finally:
                updated.set()
        with ThreadPoolExecutor(max_workers=2) as pool:
            first_result, second_result = pool.submit(first), pool.submit(second)
            self.assertEqual(first_result.result(), "original prompt")
            self.assertEqual(second_result.result(), "parallel update")
        self.assertIsNone(_metadata_reads.get())

    def test_cached_hit_rechecks_real_ancestor_junction(self):
        directory = self.storage._task_dir(self.project.project_id, self.task.task_id)
        saved, outside = self.root / "saved-task", self.root / "outside"
        outside.mkdir()
        for path in (directory, saved, outside):
            self.assertTrue(path.resolve().is_relative_to(self.root))
        with metadata_read_snapshot():
            self.get_task()
            directory.rename(saved)
            try:
                try:
                    directory.symlink_to(outside, target_is_directory=True)
                except (OSError, NotImplementedError):
                    try:
                        import _winapi
                        _winapi.CreateJunction(str(outside), str(directory))
                    except (ImportError, AttributeError, OSError) as exc:
                        self.skipTest(f"Directory links unavailable: {exc}")
                with self.assertRaises(ValueError):
                    self.get_task()
            finally:
                if directory.is_symlink():
                    directory.unlink()
                elif directory.exists():
                    directory.rmdir()
                saved.rename(directory)

    def test_ownership_rechecked_on_same_snapshot_hit_after_move(self):
        app, storage, store, manager, aid, wid, peer, pwid, main, task = self.account_fixture()
        projects = WorkspaceProjects(storage, store, aid, wid)
        scoped = WorkspaceStorage(storage, projects)
        destination = store.create_workspace(aid, "destination")["workspace_id"]
        with workspace_request(), metadata_read_snapshot():
            scoped.get_task(task.project_id, task.task_id)
            store.change_project(aid, wid, main.main_project_id, target_workspace_id=destination)
            with self.assertRaises(FileNotFoundError):
                scoped.get_task(task.project_id, task.task_id)
        peer_storage = WorkspaceStorage(storage, WorkspaceProjects(storage, store, peer, pwid))
        with workspace_request(), metadata_read_snapshot(), self.assertRaises(FileNotFoundError):
            peer_storage.get_task(task.project_id, task.task_id)

    def test_http_list_refresh_move_delete_and_write_scope(self):
        app, storage, store, manager, aid, wid, peer, pwid, main, task = self.account_fixture()
        observations = []
        original = storage.get_task
        def observed(*args, **kwargs):
            observations.append(_metadata_reads.get() is not None)
            return original(*args, **kwargs)
        with TestClient(app, base_url="http://localhost") as client, patch.object(storage, "get_task", observed):
            client.cookies.set("iw_session", "owner-session")
            response = client.get("/api/main-projects")
            self.assertEqual(response.status_code, 200, response.text)
            self.assertTrue(observations and all(observations))
            observations.clear()
            updated = client.patch(f"/api/tasks/{task.task_id}", headers={"X-Workbench-CSRF": "csrf"}, json={"name": "renamed task"})
            self.assertEqual(updated.status_code, 200, updated.text)
            self.assertTrue(observations and not any(observations))
            manager.create_task(manager.default_subproject(main.main_project_id).subproject_id, "generate", "second task")
            second = client.get("/api/main-projects").json()["main_projects"]
            self.assertEqual(second[0]["task_count"], 2)
            manager.update_main_project(main.main_project_id, name="renamed project")
            self.assertEqual(client.get("/api/main-projects").json()["main_projects"][0]["name"], "renamed project")
            destination = store.create_workspace(aid, "destination")["workspace_id"]
            store.change_project(aid, wid, main.main_project_id, target_workspace_id=destination)
            self.assertEqual(client.get("/api/main-projects").json()["main_projects"], [])
            self.assertEqual(len(client.get("/api/main-projects", headers={"X-Workspace-Id": destination}).json()["main_projects"]), 1)
            store.change_project(aid, destination, main.main_project_id, deleted=True)
            self.assertEqual(client.get("/api/main-projects", headers={"X-Workspace-Id": destination}).json()["main_projects"], [])
            client.cookies.set("iw_session", "peer-session")
            self.assertEqual(client.get("/api/main-projects").json()["main_projects"], [])


if __name__ == "__main__":
    unittest.main()
