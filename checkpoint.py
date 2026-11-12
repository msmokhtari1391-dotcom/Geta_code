"""ذخیره‌ی وضعیت کار در .geta/state.json برای ادامه بعد از لیمیت یا بستن برنامه."""
import json
import os
import shutil
import time


class Checkpoint:
    def __init__(self, ws):
        self.ws = ws
        self.path = ws.meta / "state.json"
        self.drafts = ws.meta / "drafts"
        self.data = {}

    def exists(self) -> bool:
        return self.path.is_file()

    def load(self) -> dict:
        self.data = json.loads(self.path.read_text(encoding="utf-8"))
        return self.data

    def save(self):
        self.data["updated"] = time.time()
        self.ws.ensure()
        tmp = self.path.with_name("state.json.tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def start(self, task, mode, name, roles, make_exe, auto_run):
        if self.exists():
            shutil.copy2(self.path, self.path.with_name("state.prev.json"))
        self.clear_drafts()
        self.data = {
            "version": 1, "task": task, "mode": mode, "name": name, "roles": list(roles),
            "make_exe": bool(make_exe), "auto_run": bool(auto_run),
            "status": "planning", "plan": None, "files": [], "run": "", "error": "",
            "created": time.time(),
        }
        self.save()

    def set_plan(self, plan):
        self.data["plan"] = {"summary": plan["summary"]}
        self.data["files"] = [dict(f, status="pending") for f in plan["files"]]
        self.data["run"] = plan.get("run", "")
        self.data["status"] = "working"
        self.save()

    def mark(self, index, status):
        self.data["files"][index]["status"] = status
        self.save()

    def pending(self):
        return [(i, f) for i, f in enumerate(self.data.get("files", [])) if f["status"] != "done"]

    def counts(self):
        files = self.data.get("files", [])
        return sum(1 for f in files if f["status"] == "done"), len(files)

    def pause(self, error):
        self.data["status"] = "paused"
        self.data["error"] = str(error)[:500]
        self.save()

    def finish(self):
        self.data["status"] = "done"
        self.data["error"] = ""
        self.clear_drafts()
        self.save()

    def _draft_path(self, rel):
        return self.drafts / (rel.replace("/", "__") + ".draft")

    def save_draft(self, rel, content):
        self.drafts.mkdir(parents=True, exist_ok=True)
        self._draft_path(rel).write_text(content, encoding="utf-8")

    def load_draft(self, rel):
        p = self._draft_path(rel)
        return p.read_text(encoding="utf-8") if p.is_file() else None

    def drop_draft(self, rel):
        p = self._draft_path(rel)
        if p.is_file():
            p.unlink()

    def clear_drafts(self):
        if self.drafts.is_dir():
            shutil.rmtree(self.drafts, ignore_errors=True)
