"""Browser regression check for Qwen seeds; uses fake HTTP responses, never a GPU.

Run: python tests/qwen_seed_browser.py [--browser PATH]
Requires the optional Playwright package and an installed Chromium/Edge browser.
"""
import argparse
from email import policy
from email.parser import BytesParser
import json
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright


ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--browser", default=r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
args = parser.parse_args()
jobs, submissions = {}, []
fail_next = [False]


def handle(route):
    request = route.request
    path = urlsplit(request.url).path
    if path == "/qwen-test":
        return route.fulfill(path=str(ROOT / "image_workbench/web_templates/qwen.html"), content_type="text/html")
    if path.startswith("/static/"):
        return route.fulfill(path=str(ROOT / "image_workbench/web_static" / Path(path).name))
    if path.endswith("/images/output"):
        return route.fulfill(body=b"test", content_type="image/png")
    if path == "/api/auth/config":
        result = {"csrf_token": "test"}
    elif path == "/api/auth/me":
        result = {"workspace_id": "test", "username": "test", "workspaces": [{"workspace_id": "test", "name": "test"}]}
    elif path == "/api/qwen/config":
        result = {"csrf_token": "test", "worker": {"online": True, "ready": True, "message": ""}}
    elif path == "/api/qwen/jobs" and request.method == "POST":
        content_type = request.headers["content-type"]
        mime = BytesParser(policy=policy.default).parsebytes(
            f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode() + request.post_data_buffer)
        payload = next(part.get_payload(decode=True) for part in mime.iter_parts()
                       if part.get_param("name", header="content-disposition") == "payload")
        body = json.loads(payload)
        submissions.append(body)
        jid = body["request_id"]
        if jid not in jobs:
            jobs[jid] = {
                "job_id": jid, "workspace_id": "test", "task": body, "status": "completed",
                "created_at": "2026-10-20T05:00:00Z", "inputs": [],
                "output": {"width": 64, "height": 64}, "elapsed_seconds": 1,
            }
        result = jobs[jid]
        if fail_next[0]:
            fail_next[0] = False
            return route.abort("connectionreset")
    elif path == "/api/qwen/jobs":
        result = {"jobs": list(reversed(list(jobs.values())))}
    else:
        return route.fulfill(status=404)
    route.fulfill(json=result)


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(executable_path=args.browser, headless=True)
    try:
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.add_init_script("""
            let nextSeed = 10000;
            crypto.getRandomValues = values => { values[0] = ++nextSeed; return values; };
        """)
        page.route("http://localhost/**", handle)
        page.goto("http://localhost/qwen-test")
        expect(page.locator("#qwenAccount")).to_contain_text("test")
        expect(page.locator("#qwenSeed")).to_have_value("")
        page.locator("#qwenMode").select_option("generate")

        def submit(prompt=None):
            if prompt is not None:
                page.locator("#qwenPrompt").fill(prompt)
            page.locator("#qwenSubmit").click()
            expect(page.locator("#qwenSubmit")).to_be_enabled()

        submit("first generation")
        first = submissions[-1]
        assert first["seed"] == 10001
        submit()
        assert submissions[-1] == first, "unchanged repeat must reuse the submission and its random seed"
        submit("new generation")
        assert submissions[-1]["seed"] == 10002
        assert submissions[-1]["request_id"] != first["request_id"]
        page.locator("#qwenSeed").fill("0")
        submit("explicit zero")
        assert submissions[-1]["seed"] == 0, "zero is a valid explicit seed"
        page.locator("#qwenSeed").fill("42")
        submit("explicit seed")
        assert submissions[-1]["seed"] == 42, "explicit seeds must reach the worker unchanged"

        page.get_by_role("button", name="用这张图继续编辑").click()
        expect(page.locator("#qwenMode")).to_have_value("edit")
        expect(page.locator("#qwenSeed")).to_have_value("")
        submit("edit the generated image")
        assert submissions[-1]["seed"] == 10003, "continue editing must not replay the generation seed"
        assert submissions[-1]["mode"] == "edit"

        fail_next[0] = True
        submit("lost response")
        expect(page.locator("#qwenMessage")).to_contain_text("重试")
        lost = submissions[-1]
        submit()
        assert submissions[-1] == lost, "retry after response loss must retain both request ID and seed"
        assert len(jobs) == 6, "a response-loss retry must not create another task"
        page.locator("#qwenRandom").click()
        submit("manual random button")
        assert submissions[-1]["seed"] == int(page.locator("#qwenSeed").input_value())
        assert not errors, errors
        print(json.dumps({"passed": True, "checks": [
            "automatic seed default", "fresh seed for new submission", "unchanged submission deduplication",
            "explicit seeds including zero", "fresh seed when continuing an image",
            "response-loss retry idempotency", "manual random button", "no page errors",
        ]}, ensure_ascii=False))
    finally:
        browser.close()
