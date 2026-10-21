"""模板、风格和私有案例的离线 HTTP 合同；全部生成均使用 Mock。"""

from io import BytesIO
import hashlib
import json
import unittest
from unittest.mock import Mock
from uuid import uuid4
from zipfile import ZIP_DEFLATED, ZipFile

try:
    from . import test_script_web as script_web
except ImportError:
    import test_script_web as script_web


EXAMPLE_TEXT = "《未寄出的信》\n1-1 夜 内 旧车站\n人物：阿宁、父亲\n▲阿宁捏住信封，望向远处的灯。\n阿宁：这次换我等你。\n【完】\n"


def docx_bytes(text):
    """小型、有效 DOCX，无外部素材，也不依赖生成库。"""
    from xml.sax.saxutils import escape

    stream = BytesIO()
    with ZipFile(stream, "w", ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
        archive.writestr("_rels/.rels", '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>')
        paragraphs = "".join("<w:p><w:r><w:t>" + escape(line) + "</w:t></w:r></w:p>" for line in text.splitlines())
        archive.writestr("word/document.xml", '<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>' + paragraphs + "<w:sectPr/></w:body></w:document>")
    return stream.getvalue()


def pdf_bytes(text="Reference screenplay. A letter waits at the station."):
    """一页标准 PDF，使用内置字体和可提取文字；空字符串模拟无文字页面。"""
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    content = ("BT /F1 12 Tf 72 720 Td (" + escaped + ") Tj ET").encode("ascii") if text else b""
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream",
    ]
    result = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, obj in enumerate(objects, 1):
        offsets.append(len(result))
        result.extend(str(index).encode() + b" 0 obj\n" + obj + b"\nendobj\n")
    start = len(result)
    result.extend(b"xref\n0 6\n0000000000 65535 f \n")
    for offset in offsets[1:]:
        result.extend(f"{offset:010d} 00000 n \n".encode())
    result.extend(b"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n" + str(start).encode() + b"\n%%EOF\n")
    return bytes(result)


class ScriptCreativeHTTPTests(unittest.TestCase):
    # Reuse setup/helpers without subclassing (which would rerun all old tests).
    setUp = script_web.ScriptWebTests.setUp
    client = script_web.ScriptWebTests.client
    body = script_web.ScriptWebTests.body
    quoted = script_web.ScriptWebTests.quoted
    submit = script_web.ScriptWebTests.submit

    def upload(self, filename="reference.txt", content=None, *, client=None, **changes):
        data = {"subproject_id": self.sid, "generation_type": "screenplay", "reference_roles": json.dumps(["structure", "dialogue"])}
        data.update(changes)
        return (client or self.alice).post("/api/scripts/examples", data=data, files={"file": (filename, EXAMPLE_TEXT.encode("utf-8") if content is None else content, "application/octet-stream")})

    def example(self, **changes):
        response = self.upload(**changes)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def draft(self, **changes):
        body = {"source_type": "manual", "extraction_method": "structure", "type": "screenplay", "name": "私人三幕结构"}
        body.update(changes)
        response = self.alice.post("/api/scripts/template-drafts", json=body)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def personal(self, **changes):
        draft = self.draft(**changes)
        return self.confirm_draft(draft, name="私人三幕结构")

    def confirm_draft(self, draft, **changes):
        response = self.alice.post("/api/scripts/templates", json={"draft_id": draft["draft_id"], "confirmed": True, "template": {**draft["template"], **changes}})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_system_catalog_preserves_legacy_and_loads_ten_templates_fifteen_styles(self):
        response = self.alice.get("/api/scripts/config")
        self.assertEqual(response.status_code, 200, response.text)
        config = response.json()
        self.assertEqual(len(config["templates"]), 12)
        self.assertEqual(len(config["styles"]), 15)
        self.assertEqual(len({entry["template_id"] for entry in config["templates"]}), 12)
        self.assertEqual(len({entry["style_id"] for entry in config["styles"]}), 15)
        ids = {entry["template_id"] for entry in config["templates"]}
        self.assertTrue({"basic_video_script", "episodic_screenplay_plaintext"}.issubset(ids))
        for entry in config["templates"]:
            with self.subTest(template=entry["template_id"]):
                for key in ("version", "structure", "required_fields", "optional_fields", "output_format", "duration_rules", "examples", "keywords", "origin"):
                    self.assertIn(key, entry)
                self.assertIsInstance(entry["version"], int)
                loaded = self.service.templates.load(entry["template_id"], entry["type"])
                self.assertTrue(loaded["template_text"].strip())
        for style in config["styles"]:
            for key in ("narrative_pacing", "dialogue_style", "emotional_tone", "conflict_style", "visual_language", "characterization"):
                self.assertIsInstance(style[key], str)
            for key in ("constraints", "prompt_guidance", "examples", "keywords"):
                self.assertIsInstance(style[key], list)
        self.adapter.generate.assert_not_called()

    def test_free_generation_combines_styles_example_and_user_constraints_at_old_price(self):
        example = self.example()
        config = self.alice.get("/api/scripts/config").json()
        style = config["styles"][0]
        request = self.body(template_id="none", style_ids=[style["style_id"]], example_refs=[{"example_id": example["example_id"], "version": 1, "reference_roles": ["dialogue"]}], variables={"location": "旧车站"}, constraints="必须保留用户指定的无旁白要求", conflicts_confirmed=True)
        payload, quote = self.quoted(request)
        self.assertEqual(quote["price"], 40)
        self.assertEqual(quote["shortfall"], 0)
        self.assertIsInstance(quote["conflicts"], list)
        response = self.alice.post("/api/scripts/tasks", json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        task = self.alice.get("/api/scripts/tasks/" + response.json()["task_id"]).json()
        self.assertEqual((task["status"], task["text"]), ("completed", script_web.TEXT))
        job = self.service._load(task["task_id"])
        prompt = job["generation_prompt"]
        self.assertIn("必须保留用户指定的无旁白要求", prompt)
        self.assertIn("这次换我等你", prompt)
        self.assertIn(style["name"], prompt)
        self.assertIn("旧车站", prompt)
        snapshot = json.dumps(job["creative_snapshot"], ensure_ascii=False)
        self.assertIn(example["example_id"], snapshot)
        self.assertIn(style["style_id"], snapshot)
        self.assertEqual(self.identity.wallet(self.aid)["used_credit"], 40)
        self.assertEqual(self.adapter.generate.call_count, 1)

    def test_conflicting_styles_require_explicit_confirmation_and_preserve_user_constraint(self):
        styles = self.alice.get("/api/scripts/config").json()["styles"]
        fast = next(item for item in styles if item.get("traits", {}).get("pacing") == "fast")
        slow = next(item for item in styles if item.get("traits", {}).get("pacing") == "slow")
        request = self.body(template_id="none", style_ids=[fast["style_id"], slow["style_id"]], constraints="必须全片保持慢节奏，保留真实人物停顿")
        payload, quote = self.quoted(request)
        self.assertTrue(quote["conflicts"])
        self.assertFalse(quote["can_generate"])
        response = self.alice.post("/api/scripts/tasks", json=payload)
        self.assertEqual(response.status_code, 409, response.text)
        self.adapter.generate.assert_not_called()
        self.assertEqual(self.identity.wallet(self.aid)["frozen_credit"], 0)
        accepted, confirmed = self.quoted({**request, "conflicts_confirmed": True})
        self.assertTrue(confirmed["can_generate"])
        response = self.alice.post("/api/scripts/tasks", json=accepted)
        self.assertEqual(response.status_code, 200, response.text)
        saved = self.service._load(response.json()["task_id"])
        self.assertIn("必须全片保持慢节奏，保留真实人物停顿", saved["generation_prompt"])
        self.assertEqual(self.adapter.generate.call_count, 1)

    def test_base_structure_findings_are_advisory_without_extra_inference_or_credit(self):
        self.adapter.generate.return_value = "正文中的行动尚未结束。[TRUNCATED]\n"
        task, _ = self.submit()
        detail = self.alice.get("/api/scripts/tasks/" + task["task_id"]).json()
        self.assertEqual(detail["status"], "completed")
        self.assertEqual(detail["text"], self.adapter.generate.return_value)
        report = detail["validation_report"]
        self.assertTrue(report["warnings"])
        self.assertFalse(report["auto_regenerated"])
        self.assertEqual(self.adapter.generate.call_count, 1)
        self.assertEqual(self.identity.wallet(self.aid)["used_credit"], 40)

    def test_txt_docx_pdf_upload_extracts_text_and_keeps_exact_original(self):
        cases = [("中文案例.txt", EXAMPLE_TEXT.encode("utf-8"), "这次换我等你"), ("reference.docx", docx_bytes(EXAMPLE_TEXT), "这次换我等你"), ("reference.pdf", pdf_bytes(), "Reference screenplay")]
        for filename, content, expected in cases:
            with self.subTest(filename=filename):
                example = self.example(filename=filename, content=content)
                self.assertTrue(example["example_id"].startswith("example_"))
                self.assertEqual((example["version"], example["owner_account_id"], example["workspace_id"]), (1, self.aid, self.wid))
                self.assertEqual(example["sha256"], hashlib.sha256(content).hexdigest())
                self.assertCountEqual(example["reference_roles"], ["structure", "dialogue"])
                self.assertIn(expected, example["text"])
                path = "/api/scripts/examples/" + example["example_id"]
                self.assertEqual(self.alice.get(path).json()["text"], example["text"])
                downloaded = self.alice.get(path + "/download", params={"version": 1})
                self.assertEqual(downloaded.status_code, 200, downloaded.text)
                self.assertEqual(downloaded.content, content)
        listed = self.alice.get("/api/scripts/examples", params={"subproject_id": self.sid})
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertEqual(len(listed.json()["examples"]), 3)
        self.assertTrue(all("text" not in item for item in listed.json()["examples"]))
        self.adapter.generate.assert_not_called()
        self.assertEqual(self.identity.wallet(self.aid)["used_credit"], 0)

    def test_upload_rejects_invalid_empty_oversized_and_decompression_bomb(self):
        cases = [("payload.exe", b"MZ-not-a-script"), ("empty.txt", b" \r\n\t"), ("binary.txt", b"hello\x00world"), ("invalid.txt", b"\xff\xfe\xfa"), ("fake.docx", b"not a ZIP"), ("empty.docx", docx_bytes("")), ("fake.pdf", b"not a PDF"), ("scanned.pdf", pdf_bytes("")), ("oversize.txt", b"a" * (5 * 1024 * 1024 + 1)), ("too-long.txt", b"a" * 200001), ("bomb.docx", docx_bytes("x" * (20 * 1024 * 1024)))]
        for filename, content in cases:
            with self.subTest(filename=filename):
                response = self.upload(filename, content)
                self.assertIn(response.status_code, (400, 413, 415, 422), response.text)
        listing = self.alice.get("/api/scripts/examples", params={"subproject_id": self.sid}).json()
        self.assertEqual(listing["examples"], [])
        self.adapter.generate.assert_not_called()

    def test_example_scopes_all_reads_writes_downloads_and_generation(self):
        example = self.example()
        eid = example["example_id"]
        path = "/api/scripts/examples/" + eid
        other_workspace = self.client(self.aid, self.other_wid)
        for client in (self.bobby, other_workspace):
            with self.subTest(account=client is self.bobby):
                for suffix in ("", "/download", "/download?version=1"):
                    self.assertEqual(client.get(path + suffix).status_code, 404)
                self.assertEqual(client.post(path + "/analyze", json={}).status_code, 404)
                self.assertEqual(client.patch(path, json={"expected_version": 1, "name": "越权改名"}).status_code, 404)
                self.assertEqual(client.post("/api/scripts/template-drafts", json={"source_type": "uploaded_file", "example_id": eid, "extraction_method": "structure", "type": "screenplay"}).status_code, 404)
        self.assertEqual(self.bobby.get("/api/scripts/examples", params={"subproject_id": self.sid}).status_code, 404)
        self.assertEqual(other_workspace.get("/api/scripts/examples").json()["examples"], [])
        self.assertEqual(self.upload(client=self.bobby).status_code, 404)
        self.assertEqual(self.upload(client=other_workspace).status_code, 404)
        self.assertEqual(self.alice.get(path, headers={"X-Workspace-ID": self.other_wid}).status_code, 404)
        self.assertEqual(self.alice.get(path, params={"workspace_id": self.other_wid}).status_code, 404)
        # A foreign reference cannot be injected even with one's own valid project.
        bmain = self.bobby.post("/api/main-projects", json={"request_id": uuid4().hex, "name": "另一个人的项目"}).json()
        bsid = bmain["subprojects"][0]["subproject_id"]
        response = self.bobby.post("/api/scripts/quote", json=self.body(subproject_id=bsid, template_id="none", example_refs=[{"example_id": eid, "reference_roles": ["style"]}]))
        self.assertEqual(response.status_code, 404, response.text)

    def test_analysis_preview_is_free_and_requires_patch_confirmation(self):
        example = self.example()
        path = "/api/scripts/examples/" + example["example_id"]
        original = self.alice.get(path).json()
        response = self.alice.post(path + "/analyze", json={})
        self.assertEqual(response.status_code, 200, response.text)
        preview = response.json()
        self.assertTrue(preview["requires_confirmation"])
        self.assertTrue(preview["provider"])
        self.assertTrue(preview["analysis"])
        self.assertEqual(self.alice.get(path).json(), original)
        saved = self.alice.patch(path, json={"expected_version": 1, "analysis": preview["analysis"]})
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(saved.json()["version"], 2)
        self.assertEqual(saved.json()["analysis"], preview["analysis"])
        self.assertEqual(self.alice.get(path + "/download?version=1").content, EXAMPLE_TEXT.encode("utf-8"))
        stale = self.alice.patch(path, json={"expected_version": 1, "name": "过期写入"})
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(self.identity.wallet(self.aid)["used_credit"], 0)
        self.adapter.generate.assert_not_called()

    def test_mock_analysis_and_extraction_are_advisory_until_explicit_save(self):
        from image_workbench.script_resources import MockExampleAnalyzer

        example = self.example()
        path = "/api/scripts/examples/" + example["example_id"]
        analyzer = Mock(wraps=MockExampleAnalyzer())
        analyzer.provider = "mock"
        self.service.resources.analyzer = analyzer
        response = self.alice.post(path + "/analyze", json={})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["provider"], "mock")
        analyzer.analyze.assert_called_once_with(example["text"], "screenplay")
        self.assertEqual(self.alice.get(path).json()["version"], 1)
        self.assertIsNone(self.alice.get(path).json()["analysis"])
        draft = self.draft(source_type="uploaded_file", example_id=example["example_id"], extraction_method="ai")
        self.assertEqual(draft["provider"], "mock")
        analyzer.extract_template.assert_called_once_with(example["text"], "screenplay")
        self.assertEqual(self.alice.get("/api/scripts/templates").json()["templates"], [])
        saved = self.confirm_draft(draft)
        self.assertEqual((saved["analysis_model"], saved["extraction_method"]), ("mock", "ai"))
        self.assertEqual(self.identity.wallet(self.aid)["used_credit"], 0)
        self.adapter.generate.assert_not_called()

    def test_mock_ai_failure_preserves_uploaded_and_existing_originals_and_manual_path(self):
        from image_workbench.script_resources import MockExampleAnalyzer

        example = self.example()
        path = "/api/scripts/examples/" + example["example_id"]
        original = self.alice.get(path).json()
        task, _ = self.submit()
        task_path = "/api/scripts/tasks/" + task["task_id"]
        artifact_before = self.alice.get(task_path).json()["output_artifact_id"]
        self.service.resources.analyzer = MockExampleAnalyzer(error="offline failure")
        response = self.alice.post(path + "/analyze", json={})
        self.assertIn(response.status_code, (400, 422, 502, 503), response.text)
        for source in ({"source_type": "uploaded_file", "example_id": example["example_id"]}, {"source_type": "existing_artifact", "task_id": task["task_id"]}):
            with self.subTest(source=source["source_type"]):
                response = self.alice.post("/api/scripts/template-drafts", json={**source, "extraction_method": "ai", "type": "screenplay"})
                self.assertIn(response.status_code, (400, 422, 502, 503), response.text)
        self.assertEqual(self.alice.get(path).json(), original)
        self.assertEqual(self.alice.get(path + "/download").content, EXAMPLE_TEXT.encode("utf-8"))
        self.assertEqual(self.alice.get(task_path + "/download").content, script_web.TEXT.encode("utf-8"))
        self.assertEqual(self.alice.get(task_path).json()["output_artifact_id"], artifact_before)
        self.assertEqual(self.alice.get("/api/scripts/templates").json()["templates"], [])
        saved = self.personal()
        self.assertEqual(saved["source_type"], "manual")
        self.assertEqual(self.identity.wallet(self.aid)["used_credit"], 40)
        self.assertEqual(self.adapter.generate.call_count, 1)

    def test_example_versions_preserve_content_and_allow_role_change_and_disable(self):
        example = self.example()
        path = "/api/scripts/examples/" + example["example_id"]
        changed = self.alice.patch(path, json={"expected_version": 1, "name": "已整理的案例", "reference_roles": ["format"], "enabled": False})
        self.assertEqual(changed.status_code, 200, changed.text)
        value = changed.json()
        self.assertEqual((value["version"], value["name"], value["reference_roles"], value["enabled"]), (2, "已整理的案例", ["format"], False))
        self.assertEqual(value["text"], example["text"])
        self.assertEqual(value["sha256"], example["sha256"])
        self.assertEqual(self.alice.get(path + "/download?version=1").content, self.alice.get(path + "/download?version=2").content)
        response = self.alice.post("/api/scripts/quote", json=self.body(template_id="none", example_refs=[{"example_id": example["example_id"], "reference_roles": ["format"]}]))
        self.assertIn(response.status_code, (400, 409), response.text)

    def test_example_patch_cannot_replace_source_text_digest_or_owner(self):
        example = self.example()
        path = "/api/scripts/examples/" + example["example_id"]
        for forged in ({"text": "被替换的故事正文"}, {"sha256": "0" * 64}, {"owner_account_id": self.bid}, {"workspace_id": self.other_wid}):
            with self.subTest(field=next(iter(forged))):
                response = self.alice.patch(path, json={"expected_version": 1, **forged})
                self.assertIn(response.status_code, (400, 422), response.text)
        self.assertEqual(self.alice.get(path).json(), example)
        self.assertEqual(self.alice.get(path + "/download").content, EXAMPLE_TEXT.encode("utf-8"))

    def test_manual_draft_is_not_saved_without_confirmation_and_repeat_save_is_idempotent(self):
        draft = self.draft()
        self.assertIn("source_preview", draft)
        self.assertIn("provider", draft)
        self.assertIn("notice", draft)
        self.assertEqual(self.alice.get("/api/scripts/templates").json()["templates"], [])
        body = {"draft_id": draft["draft_id"], "confirmed": False, "template": {**draft["template"], "name": "确认后的结构"}}
        rejected = self.alice.post("/api/scripts/templates", json=body)
        self.assertIn(rejected.status_code, (400, 409, 422), rejected.text)
        self.assertEqual(self.alice.get("/api/scripts/templates").json()["templates"], [])
        body["confirmed"] = True
        saved = self.alice.post("/api/scripts/templates", json=body)
        self.assertEqual(saved.status_code, 200, saved.text)
        template = saved.json()
        self.assertTrue(template["template_id"].startswith("personal_"))
        self.assertEqual((template["name"], template["version"], template["visibility"], template["owner_account_id"], template["workspace_id"]), ("确认后的结构", 1, "private", self.aid, self.wid))
        repeated = self.alice.post("/api/scripts/templates", json=body)
        self.assertEqual(repeated.status_code, 200, repeated.text)
        self.assertEqual(repeated.json()["template_id"], template["template_id"])
        self.assertEqual(len(self.alice.get("/api/scripts/templates").json()["templates"]), 1)
        self.adapter.generate.assert_not_called()

    def test_uploaded_source_draft_extracts_structure_without_copying_work_body(self):
        example = self.example()
        draft = self.draft(source_type="uploaded_file", example_id=example["example_id"])
        template = draft["template"]
        self.assertTrue(template["structure"])
        self.assertTrue(all(isinstance(item, dict) and "section" in item and "purpose" in item for item in template["structure"]))
        self.assertNotIn("这次换我等你", json.dumps(template, ensure_ascii=False))
        self.assertIn("这次换我等你", json.dumps(draft["source_preview"], ensure_ascii=False))
        self.assertEqual(self.alice.get("/api/scripts/templates").json()["templates"], [])
        saved = self.confirm_draft(draft)
        self.assertEqual(saved["source_example_id"], example["example_id"])
        self.assertEqual(saved["source_type"], "uploaded_file")
        self.assertEqual(saved["source_version"], 1)
        self.assertEqual(self.alice.get("/api/scripts/examples/" + example["example_id"] + "/download").content, EXAMPLE_TEXT.encode("utf-8"))

    def test_existing_script_and_screenplay_drafts_retain_source_and_original_artifact(self):
        for generation_type, template_id in (("script", "basic_video_script"), ("screenplay", "episodic_screenplay_plaintext")):
            with self.subTest(generation_type=generation_type):
                task, _ = self.submit(self.body(generation_type=generation_type, template_id=template_id))
                path = "/api/scripts/tasks/" + task["task_id"]
                original_detail = self.alice.get(path).json()
                original_bytes = self.alice.get(path + "/download").content
                original_artifact = self.storage.get_artifact(original_detail["output_artifact_id"])
                draft = self.draft(source_type="existing_artifact", task_id=task["task_id"], type=generation_type)
                template = draft["template"]
                self.assertEqual(template["type"], generation_type)
                self.assertNotIn("小林打开台灯", json.dumps(template, ensure_ascii=False))
                saved = self.confirm_draft(draft)
                self.assertEqual(saved["source_artifact_id"], original_detail["output_artifact_id"])
                self.assertEqual(saved["source_version"], original_detail["output_version"])
                self.assertEqual(self.alice.get(path + "/download").content, original_bytes)
                self.assertEqual(self.storage.get_artifact(original_detail["output_artifact_id"]), original_artifact)
                self.assertEqual(self.alice.get(path).json()["version_id"], original_detail["version_id"])
        self.assertEqual(self.identity.wallet(self.aid)["used_credit"], 80)
        self.assertEqual(self.adapter.generate.call_count, 2)

    def test_personal_template_edit_copy_disable_and_historical_versions(self):
        original = self.personal()
        path = "/api/scripts/templates/" + original["template_id"]
        changes = {"name": "修订结构", "structure": [{"section": "开场", "purpose": "建立明确目标"}, {"section": "结尾", "purpose": "完成行动"}], "variables": [{"name": "location", "label": "地点", "default": "车站"}], "enabled": False}
        updated = self.alice.patch(path, json={"expected_version": 1, "template": changes})
        self.assertEqual(updated.status_code, 200, updated.text)
        latest = updated.json()
        self.assertEqual((latest["version"], latest["name"], latest["enabled"]), (2, "修订结构", False))
        self.assertEqual(latest["variables"], changes["variables"])
        self.assertEqual(self.alice.patch(path, json={"expected_version": 1, "template": {"name": "过期修改"}}).status_code, 409)
        history = self.alice.get(path + "/versions/1")
        self.assertEqual(history.status_code, 200, history.text)
        self.assertEqual((history.json()["version"], history.json()["name"], history.json()["enabled"]), (1, original["name"], True))
        self.assertEqual(len(self.alice.get(path).json()["versions"]), 2)
        self.assertEqual(self.alice.get(path + "/versions/999").status_code, 404)
        active_ids = {item["template_id"] for item in self.alice.get("/api/scripts/config").json()["templates"]}
        self.assertNotIn(original["template_id"], active_ids)
        all_ids = {item["template_id"] for item in self.alice.get("/api/scripts/templates?include_disabled=true").json()["templates"]}
        self.assertIn(original["template_id"], all_ids)
        copied = self.alice.post(path + "/copy", json={"name": "独立副本"})
        self.assertEqual(copied.status_code, 200, copied.text)
        duplicate = copied.json()
        self.assertNotEqual(duplicate["template_id"], original["template_id"])
        self.assertEqual((duplicate["version"], duplicate["name"]), (1, "独立副本"))
        cpath = "/api/scripts/templates/" + duplicate["template_id"]
        response = self.alice.patch(cpath, json={"expected_version": 1, "template": {"name": "副本自己的名称"}})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.alice.get(path).json()["name"], "修订结构")

    def test_personal_drafts_templates_and_history_cannot_cross_account_or_workspace(self):
        draft = self.draft()
        personal = self.personal()
        path = "/api/scripts/templates/" + personal["template_id"]
        other_workspace = self.client(self.aid, self.other_wid)
        for client in (self.bobby, other_workspace):
            with self.subTest(account=client is self.bobby):
                self.assertEqual(client.get("/api/scripts/templates?include_disabled=true").json()["templates"], [])
                for suffix in ("", "/versions/1"):
                    self.assertEqual(client.get(path + suffix).status_code, 404)
                self.assertEqual(client.patch(path, json={"expected_version": 1, "template": {"name": "越权"}}).status_code, 404)
                self.assertEqual(client.post(path + "/copy", json={}).status_code, 404)
                self.assertEqual(client.post("/api/scripts/templates", json={"draft_id": draft["draft_id"], "confirmed": True, "template": {}}).status_code, 404)
                own_project = client.post("/api/main-projects", json={"request_id": uuid4().hex, "name": "独立空间的项目"})
                self.assertEqual(own_project.status_code, 200, own_project.text)
                own_sid = own_project.json()["subprojects"][0]["subproject_id"]
                response = client.post("/api/scripts/quote", json=self.body(subproject_id=own_sid, template_id=personal["template_id"]))
                self.assertEqual(response.status_code, 404, response.text)
        self.assertEqual(self.alice.get(path, headers={"X-Workspace-ID": self.other_wid}).status_code, 404)

    def test_existing_artifact_cannot_be_used_as_template_source_outside_its_scope(self):
        task, _ = self.submit()
        body = {"source_type": "existing_artifact", "task_id": task["task_id"], "extraction_method": "structure", "type": "screenplay"}
        for client in (self.bobby, self.client(self.aid, self.other_wid)):
            response = client.post("/api/scripts/template-drafts", json=body)
            self.assertEqual(response.status_code, 404, response.text)
        response = self.alice.post("/api/scripts/template-drafts", json={**body, "type": "script"})
        self.assertIn(response.status_code, (400, 422), response.text)
        self.assertEqual(self.alice.get("/api/scripts/tasks/" + task["task_id"] + "/download").content, script_web.TEXT.encode("utf-8"))

    def test_source_and_owner_fields_cannot_be_forged_on_save_or_patch(self):
        draft = self.draft()
        forged = {"source_type": "existing_artifact", "source_artifact_id": uuid4().hex, "owner_account_id": self.bid, "workspace_id": self.bwid, "visibility": "public"}
        response = self.alice.post("/api/scripts/templates", json={"draft_id": draft["draft_id"], "confirmed": True, "template": {**draft["template"], **forged}})
        self.assertIn(response.status_code, (400, 422), response.text)
        personal = self.personal()
        path = "/api/scripts/templates/" + personal["template_id"]
        response = self.alice.patch(path, json={"expected_version": 1, "template": forged})
        self.assertIn(response.status_code, (400, 422), response.text)
        value = self.alice.get(path).json()
        self.assertEqual((value["version"], value["owner_account_id"], value["workspace_id"]), (1, self.aid, self.wid))

    def test_quote_changes_after_personal_or_example_version_change(self):
        personal = self.personal()
        example = self.example()
        request = self.body(template_id=personal["template_id"], example_refs=[{"example_id": example["example_id"], "reference_roles": ["dialogue"]}], conflicts_confirmed=True)
        payload, before = self.quoted(request)
        path = "/api/scripts/templates/" + personal["template_id"]
        changed = self.alice.patch(path, json={"expected_version": 1, "template": {"name": "新的模板版本", "prompt_guidance": ["只写清晰可见的动作"]}})
        self.assertEqual(changed.status_code, 200, changed.text)
        response = self.alice.post("/api/scripts/tasks", json=payload)
        self.assertEqual(response.status_code, 409, response.text)
        payload2, after_template = self.quoted(request)
        self.assertNotEqual(before["quote_signature"], after_template["quote_signature"])
        analysis = self.alice.post("/api/scripts/examples/" + example["example_id"] + "/analyze", json={}).json()["analysis"]
        analysis["structure"] = "用户确认的第二版分析"
        changed = self.alice.patch("/api/scripts/examples/" + example["example_id"], json={"expected_version": 1, "analysis": analysis})
        self.assertEqual(changed.status_code, 200, changed.text)
        response = self.alice.post("/api/scripts/tasks", json=payload2)
        self.assertEqual(response.status_code, 409, response.text)
        _, after_example = self.quoted(request)
        self.assertNotEqual(after_template["quote_signature"], after_example["quote_signature"])
        self.assertEqual(self.identity.wallet(self.aid)["used_credit"], 0)
        self.assertEqual(self.identity.wallet(self.aid)["frozen_credit"], 0)
        self.adapter.generate.assert_not_called()

    def test_queued_task_freezes_old_template_example_and_prompt_after_edit_disable(self):
        personal = self.personal()
        example = self.example()
        payload, _ = self.quoted(self.body(template_id=personal["template_id"], template_version=1, example_refs=[{"example_id": example["example_id"], "version": 1, "reference_roles": ["dialogue"]}], conflicts_confirmed=True))
        queued = self.service.submit(self.aid, self.wid, payload)
        tid = queued["task_id"]
        before = self.service._load(tid)
        self.assertEqual(before["status"], "queued")
        frozen = before["creative_snapshot"]
        response = self.alice.patch("/api/scripts/templates/" + personal["template_id"], json={"expected_version": 1, "template": {"name": "不可影响已排队任务的新名字", "enabled": False, "prompt_guidance": ["后来才增加的规则"]}})
        self.assertEqual(response.status_code, 200, response.text)
        analysis = self.alice.post("/api/scripts/examples/" + example["example_id"] + "/analyze", json={}).json()["analysis"]
        analysis["structure"] = "后来才保存的分析"
        response = self.alice.patch("/api/scripts/examples/" + example["example_id"], json={"expected_version": 1, "name": "后来才改变的案例", "enabled": False, "analysis": analysis})
        self.assertEqual(response.status_code, 200, response.text)
        self.adapter.generate.assert_not_called()
        response = self.alice.post("/api/scripts/tasks/" + tid + "/run", json={})
        self.assertEqual(response.status_code, 200, response.text)
        after = self.service._load(tid)
        self.assertEqual(after["status"], "completed")
        self.assertEqual(after["creative_snapshot"], frozen)
        self.assertEqual(after["generation_prompt"], before["generation_prompt"])
        self.assertNotIn("后来才", after["generation_prompt"])
        detail = self.alice.get("/api/scripts/tasks/" + tid).json()
        self.assertEqual(detail["text"], script_web.TEXT)
        version = self.service._versions()["versions"][detail["version_id"]]
        self.assertIn(personal["template_id"], json.dumps(version, ensure_ascii=False))
        self.assertIn(example["example_id"], json.dumps(version, ensure_ascii=False))
        self.assertEqual(self.identity.wallet(self.aid)["used_credit"], 40)
        self.assertEqual(self.adapter.generate.call_count, 1)

    def test_recommendation_is_advisory_free_and_does_not_apply_or_generate(self):
        response = self.alice.post("/api/scripts/recommend", json={"input_request": "写一个温暖的亲情短片，有明确分场和对白", "generation_type": "screenplay"})
        self.assertEqual(response.status_code, 200, response.text)
        value = response.json()
        for key in ("recommended_template", "recommended_styles", "alternatives", "match_score", "score_method", "reasons", "missing_information", "provider"):
            self.assertIn(key, value)
        self.assertIsInstance(value["recommended_template"], dict)
        self.assertIn("template_id", value["recommended_template"])
        self.assertTrue(all(isinstance(item, dict) and "style_id" in item for item in value["recommended_styles"]))
        self.assertTrue(value["reasons"])
        self.assertEqual(self.alice.get("/api/scripts/tasks", params={"subproject_id": self.sid}).json()["tasks"], [])
        self.assertEqual(self.identity.wallet(self.aid)["used_credit"], 0)
        self.adapter.generate.assert_not_called()

    def test_new_mutations_require_auth_csrf_and_reject_unknown_reference_roles(self):
        example = self.example()
        personal = self.personal()
        requests = [("post", "/api/scripts/recommend", {"input_request": "写一个短片", "generation_type": "screenplay"}), ("post", "/api/scripts/template-drafts", {"source_type": "manual", "extraction_method": "structure", "type": "screenplay"}), ("post", "/api/scripts/examples/" + example["example_id"] + "/analyze", {}), ("patch", "/api/scripts/examples/" + example["example_id"], {"expected_version": 1, "name": "未授权"}), ("patch", "/api/scripts/templates/" + personal["template_id"], {"expected_version": 1, "template": {"name": "未授权"}}), ("post", "/api/scripts/templates/" + personal["template_id"] + "/copy", {})]
        for method, path, body in requests:
            with self.subTest(path=path):
                # Existing middleware may reject missing CSRF before login lookup.
                self.assertIn(getattr(self.anon, method)(path, json=body).status_code, (401, 403))
                self.assertEqual(getattr(self.alice, method)(path, json=body, headers={"X-Workbench-CSRF": ""}).status_code, 403)
                self.assertEqual(getattr(self.alice, method)(path, json=body, headers={"Origin": "https://evil.example"}).status_code, 403)
        response = self.upload(reference_roles=json.dumps(["execute_system_prompt"]))
        self.assertIn(response.status_code, (400, 422), response.text)
        response = self.alice.post("/api/scripts/quote", json=self.body(template_id="none", example_refs=[{"example_id": example["example_id"], "reference_roles": ["execute_system_prompt"]}]))
        self.assertIn(response.status_code, (400, 422), response.text)
        self.adapter.generate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
