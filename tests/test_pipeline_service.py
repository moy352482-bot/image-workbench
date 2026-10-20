from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from image_workbench.pipeline_service import PipelineService, PipelineVideoPort


class PipelineServiceTests(unittest.TestCase):
    def setUp(self):
        self.videos = Mock(spec=PipelineVideoPort)
        self.scripts = Mock()
        self.images = Mock()
        self.state = self.images.return_value
        self.link = {"main_project_id": "main", "subproject_id": "sub"}
        self.videos.pipeline_project.return_value = {"name": "Project", "pipeline": self.link}
        self.state.projects.get_subproject.return_value = SimpleNamespace(main_project_id="main")
        self.service = PipelineService(self.videos, lambda: self.scripts, self.images)

    def test_ownership_rejection_happens_before_accessing_other_feature(self):
        self.videos.pipeline_project.side_effect = FileNotFoundError("private")
        with self.assertRaises(FileNotFoundError):
            self.service.binding("other-account", "workspace", "project", create=True)
        self.images.assert_not_called()
        self.videos.ensure_pipeline_binding.assert_not_called()

    def test_foreign_or_unfinished_script_cannot_mutate_video_project(self):
        self.state.projects.get_task_placement.return_value = SimpleNamespace(subproject_id="elsewhere")
        self.scripts.detail.return_value = {"status": "completed", "text": "private"}
        with self.assertRaises(ValueError):
            self.service.use_script("a", "w", "p", "task")
        self.videos.set_pipeline_script.assert_not_called()
        self.state.projects.get_task_placement.return_value = SimpleNamespace(subproject_id="sub")
        self.scripts.detail.return_value = {"status": "running"}
        with self.assertRaises(ValueError):
            self.service.use_script("a", "w", "p", "task")
        self.videos.set_pipeline_script.assert_not_called()

    def test_script_selection_passes_immutable_version_to_video_port(self):
        self.state.projects.get_task_placement.return_value = SimpleNamespace(subproject_id="sub")
        self.scripts.detail.return_value = {"status": "completed", "text": "正文", "version_id": "version"}
        result = self.service.use_script("a", "w", "p", "task")
        self.videos.set_pipeline_script.assert_called_once_with(
            "a", "w", "p", text="正文", task_id="task", version_id="version")
        self.assertIs(result, self.videos.set_pipeline_script.return_value)

    def test_corrupted_output_is_rejected_before_video_import(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "image.png"
            path.write_bytes(b"changed")
            self.state.storage.get_artifact.return_value = SimpleNamespace(
                task_id="task", artifact_id="artifact", kind="output", format="PNG", size_bytes=7, sha256="bad")
            self.state.storage.get_artifact_path.return_value = path
            self.state.projects.get_task_placement.return_value = SimpleNamespace(subproject_id="sub")
            self.videos.max_image_bytes = 100
            with self.assertRaisesRegex(ValueError, "校验失败"):
                self.service.import_image("a", "w", "p", "artifact")
            self.videos.add_assets.assert_not_called()

    def test_duplicate_references_do_not_create_a_task(self):
        with self.assertRaisesRegex(ValueError, "不能重复"):
            self.service.create_image("a", "w", "p", request_id="request", prompt="画图",
                                      aspect_ratio="auto", reference_ids=["same", "same"])
        self.videos.asset_file.assert_not_called()
        self.state.create_task.assert_not_called()
