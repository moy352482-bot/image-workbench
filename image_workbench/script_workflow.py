"""Coordinate script -> storyboard stages through public, durable service ports.

Services retain their own transactions and recovery. Neither retains or invokes
this coordinator; local execution, remote completion and startup call it here.
"""
import logging

from .storyboard_service import stable

LOG = logging.getLogger(__name__)


class ScriptStoryboardWorkflow:
    def __init__(self, get_scripts, get_storyboards):
        self.get_scripts = get_scripts
        self.get_storyboards = get_storyboards

    def _options(self, body):
        raw = body.get("storyboard_options")
        return self.get_storyboards().normalize_options(raw) if raw is not None else None

    def validate_input(self, aid, wid, body):
        return self.get_scripts().validate_input(aid, wid, body, storyboard_options=self._options(body))

    def _plan(self, aid, wid, body, options):
        if options is None:
            return None
        request = self.get_scripts().validate_input(aid, wid, body, storyboard_options=options)
        return {"options": options, "quote": self.get_storyboards().quote_options(aid, wid, options, request=request)}

    def quote(self, aid, wid, body):
        plan = self._plan(aid, wid, body, self._options(body))
        return self.get_scripts().quote(aid, wid, body, storyboard_plan=plan)

    def submit(self, aid, wid, body, *, admission=None):
        scripts = self.get_scripts()
        options = self._options(body)
        if options is None:
            return scripts.submit(aid, wid, body, admission=admission)
        old = scripts.replay_submission(aid, wid, body, storyboard_options=options)
        if old is not None:
            return self._joined(old)
        if admission is not None:
            admission()
        plan = self._plan(aid, wid, body, options)
        return self._joined(scripts.submit(aid, wid, body, admission=admission, storyboard_plan=plan))

    def _joined(self, script):
        if script.get("storyboard_options"):
            stage = self.get_storyboards().dependency_task(script["account_id"], script["workspace_id"], script)
            return {**script, "storyboard_task": stage, "storyboard_task_id": stage["task_id"] if stage else None}
        return script

    def detail(self, aid, wid, tid):
        return self._joined(self.get_scripts().detail(aid, wid, tid))

    def list_tasks(self, aid, wid, subproject_id=None):
        return [self._joined(task) for task in self.get_scripts().list_tasks(aid, wid, subproject_id)]

    def run(self, aid, wid, tid):
        return self.after_script(self.get_scripts().run(aid, wid, tid))

    def recover(self, aid, wid, tid):
        return self.after_script(self.get_scripts().recover(aid, wid, tid))

    def after_script(self, script):
        try:
            self.dependency_completed(script)
        except Exception:
            # TXT/credit is already committed. Keep the durable intent for startup
            # even if the planner's failure record cannot currently be written.
            LOG.exception("Storyboard handoff deferred for script %s", script["task_id"])
        return self._joined(script)

    def dependency_completed(self, script):
        if (script.get("status") != "completed" or not script.get("storyboard_options")
                or not script.get("storyboard_confirmed") or script.get("source") == "manual_edit"):
            return None
        boards = self.get_storyboards()
        aid, wid = script["account_id"], script["workspace_id"]
        task = boards.dependency_task(aid, wid, script)
        if task is not None:
            return boards.run(aid, wid, task["task_id"]) if task["status"] == "queued" else task
        try:
            task = boards.submit(aid, wid, {**script["storyboard_options"], "source_script_version": script["version_id"],
                                           "request_id": stable("dependency:" + script["task_id"]), "confirmed": True}, dependency=script)
            return boards.run(aid, wid, task["task_id"])
        except Exception as exc:
            return boards.record_dependency_failure(script, exc)

    def resume_pending(self):
        for script in self.get_scripts().completed_storyboard_dependencies():
            self.after_script(script)
        self.get_storyboards().resume_pending()
