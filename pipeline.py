"""اجرای ساخت جدید / ویرایش / ادامه با ذخیره‌ی وضعیت بعد از هر مرحله."""
import ast
import os
import re
import shutil
import subprocess
import sys
import time
import webbrowser

import checks
import prompts
import cancel
import devlog
from errors import NeedsLogin, ReadFailed, ConversationFull
from checkpoint import Checkpoint
from webbundle import BundleError, bundle_project
from textparse import extract_file, extract_json, parse_review, declared_format, strip_echo
from geta_code import prefer_for_file
from workspace import Workspace


PLACEHOLDER_MARKERS = (
    "relative/path", "path.ext", "...full file content", "exact responsibilities",
    "command to run the project", "key classes/functions and how files connect",
    "architecture, key classes", "rest unchanged",
    "tic-tac-toe game: main.py runs the loop", "class Board with draw()",
    "write the complete code of ",
)


def is_placeholder(text) -> bool:
    low = str(text or "").lower()
    return any(m in low for m in PLACEHOLDER_MARKERS)


RESEND_MARKERS = ("incomplete", "truncated", "resend", "please provide the complete", "missing",
                  "ناقص", "دوباره بفرست", "کامل نیست", "قطع شده")


def is_placeholder_content(content) -> bool:
    s = str(content or "").strip()
    return bool(s) and len(s) <= 60 and "PLACEHOLDER" in s.upper()


def reviewer_wants_rewrite(text) -> bool:
    low = str(text or "").lower()
    if "@@@ok" in low or low.strip() == "ok":
        return False
    return any(m in low for m in RESEND_MARKERS)


def check_syntax(path: str, content: str) -> str:
    """بررسی سینتکس فایل قبل از ذخیره. اگر سالم باشد رشته‌ی خالی برمی‌گرداند."""
    ext = os.path.splitext(path)[1].lower()
    if ext in (".py", ".pyw"):
        try:
            ast.parse(content, filename=path)
        except SyntaxError as e:
            return f"خطای سینتکس پایتون در خط {e.lineno}: {e.msg}"
    elif ext in (".html", ".htm"):
        low = content.lower()
        if "<html" in low and "</html>" not in low:
            return "فایل HTML ناقص است (تگ پایانی </html> وجود ندارد)"
        if low.count("<script") > low.count("</script>"):
            return "تگ <script> در فایل HTML بسته نشده است"
    return ""


def output_format(task, make_exe=False):
    """فرمت نهایی را از خواسته‌ی کاربر می‌خواند. اگه چیزی نخواسته باشه، None برمی‌گرداند."""
    low = (task or "").lower()
    if make_exe or re.search(r"\bexe\b", low) or "فایل اجرایی" in low:
        return "exe"
    if re.search(r"\bhtml\b", low) or any(w in low for w in ("مرورگر", "browser", "وب", "web", "سایت", "website", "three.js", "canvas")):
        return "html"
    if re.search(r"\bzip\b", low) or "زیپ" in low:
        return "zip"
    return None


REFINE_WITH_AI = False


def _issue_text(issues):
    out = []
    for it in issues[:12]:
        if it["kind"] == "module_missing":
            out.append(f"ماژول {it['module']} (در {it['file']})")
        else:
            out.append(f"نام {it['name']} در {it['module']} (در {it['file']})")
    return "؛ ".join(out)


class BuildPaused(Exception):
    pass


class Pipeline:
    def __init__(self, ask, log, progress, auto_resume_wait=600, max_auto_resumes=3, on_output=None):
        self.on_output = on_output
        self.ask = ask
        self.log = log
        self.progress = progress
        self.auto_resume_wait = auto_resume_wait
        self.max_auto_resumes = max_auto_resumes
        self._durations = []
        self.review = False
        self.cp = None

    def run(self, folder, mode, task, name, roles, make_exe, auto_run) -> bool:
        ws = Workspace(folder)
        self._ws, self._reply_path = ws, None
        devlog.set_session(name or ws.root.name)
        devlog.reset_latest(f"پروژه: {name or ws.root.name} | حالت: {mode} | تسک: {task[:200]}")
        devlog.event("PIPELINE_START", f"شروع پایپ‌لاین در پوشه {ws.root}", {"mode": mode, "name": name, "roles": roles, "make_exe": make_exe, "auto_run": auto_run, "task": task})
        ws.root.mkdir(parents=True, exist_ok=True)
        ws.ensure()
        cp = self.cp = Checkpoint(ws)

        if mode == "resume":
            if not cp.exists():
                self.log("✗ خطا: در این پوشه کار نیمه‌تمامی پیدا نشد.")
                return False
            d = cp.load()
            if d.get("status") == "done":
                self.log("✓ کار این پوشه قبلاً کامل شده است.")
                return True
            task, name, roles = d["task"], d.get("name") or name, d.get("roles") or roles
            make_exe, auto_run, mode = d.get("make_exe", make_exe), d.get("auto_run", auto_run), d["mode"]
            done, total = cp.counts()
            self.log(f"↻ ادامه از جای قبلی: {done} از {total} فایل انجام شده")
        else:
            if cp.exists():
                try:
                    if cp.load().get("status") != "done":
                        self.log("⚠ یک کار نیمه‌تمام در این پوشه بود؛ با کار جدید جایگزین شد (state.prev.json).")
                except Exception:
                    pass
            cp.start(task, mode, name, roles, make_exe, auto_run)
            (ws.meta / "conv.json").unlink(missing_ok=True)

        self.review = "reviewer" in (roles or [])
        name = name or ws.root.name
        self.log(f"📁 پوشه: {ws.root}  ·  حالت: {'ویرایش' if mode == 'edit' else 'ساخت جدید'}")

        stored = cp.data.get("spec") or ""
        brief = task + ("\n\n[توضیح تکمیلی تولیدشده توسط هوش مصنوعی — راهنما، نه الزام اضافه]\n" + stored if stored else "")
        self._cp, self._brief = cp, brief
        try:
            if not cp.data.get("files"):
                self._report("تکمیل توضیح پروژه")
                spec = self._refine(task, cp, mode)
                brief = task + ("\n\n[توضیح تکمیلی تولیدشده توسط هوش مصنوعی — راهنما، نه الزام اضافه]\n" + spec if spec else "")
                self._report("برنامه‌ریزی")
                plan = self._plan(ws, mode, brief, name)
                cp.set_plan(plan)
                self._note("نقشه:\n" + "\n".join(f"- {f['action']}: {f['path']} — {f['goal'][:150]}" for f in plan["files"]))
                self.log(f"✓ نقشه آماده شد: {len(plan['files'])} فایل")
                for f in plan["files"]:
                    self.log(f"  • {f['action']}: {f['path']}")
            self._process_files(ws, cp, brief)
            self._verify(ws, cp)
            left = self._resolve_static(ws, cp, brief)
            if left:
                raise RuntimeError("import های محلی هنوز حل نشده‌اند: " + _issue_text(left))
        except cancel.BuildCancelled:
            return self._stopped(cp)
        except BuildPaused as e:
            return self._paused(cp, e)
        except Exception as e:
            return self._paused(cp, e)

        task_text = cp.data.get("task", task)
        req_fmt = output_format(task_text, make_exe)
        if not req_fmt and self._is_web(ws, task_text):
            req_fmt = "html"

        self._integrate(ws, cp, brief, req_fmt)
        left = self._resolve_static(ws, cp, brief)
        if left:
            return self._paused(cp, RuntimeError("import های محلی بعد از یکپارچه‌سازی ناقص‌اند: " + _issue_text(left)))
        gate = self._final_gate(ws, cp)
        if gate:
            for idx, f in enumerate(cp.data.get("files") or []):
                if f.get("path") in gate:
                    cp.mark(idx, "pending")
            return self._paused(cp, RuntimeError("فایل‌های ناقص یا قالبی (دوباره نوشته می‌شوند): " + ", ".join(gate)))
        cp.finish()
        self.log("✓ همه‌ی فایل‌ها نوشته شدند.")
        self.progress(96, 0, "ساخت خروجی نهایی")

        run_cmd = cp.data.get("run", "")
        fmt = req_fmt or cp.data.get("build_kind")
        if not fmt and self._is_web(ws, task_text):
            fmt = "html"

        if not self._is_web(ws, task_text):
            self._install_requirements(ws)
            self._test_python_entry(ws, cp, brief, fmt)

        bundle = None
        exe_file = None
        zip_file = None

        if fmt == "html" or self._is_web(ws, task_text):
            bundle = self._deliver_web(ws, name, cp, brief)
        elif fmt == "exe":
            ok_build, tail, exe_file = self._build_exe(ws, run_cmd, name, cp.data.get("entry"))
            for attempt in range(2):
                if ok_build:
                    break
                self._repair(ws, cp, brief, fmt, tail, attempt + 1)
                ok_build, tail, exe_file = self._build_exe(ws, run_cmd, name, cp.data.get("entry"))
            if ok_build and exe_file:
                self._emit({"kind": "exe", "path": str(exe_file), "folder": str(ws.root), "title": "فایل اجرایی (EXE) آماده است"})
        elif fmt == "zip":
            zip_file = self._make_zip(ws, name)
            if zip_file:
                self._emit({"kind": "zip", "path": str(zip_file), "folder": str(ws.root), "title": "فایل زیپ آماده است"})
        else:
            self.log("📂 کدها در پوشه‌ی پروژه آماده‌اند.")

        self.progress(100, 0, "تمام شد")
        if not (bundle or exe_file or zip_file):
            self._emit_folder("پروژه آماده است")

        if auto_run:
            if bundle:
                self._open_file(bundle)
            elif exe_file and exe_file.is_file():
                self._open_file(exe_file)
            else:
                self._run_project(ws, run_cmd, cp.data.get("entry"))
        return True

    def _verify(self, ws, cp):
        for item in cp.data.get("files") or []:
            path = item.get("path", "")
            if is_placeholder(path):
                raise RuntimeError(f"نام فایل قالبی است و ذخیره نشد: {path}")
            if item.get("action") == "delete":
                continue
            if not ws.exists(path):
                raise RuntimeError(f"فایل نوشته نشده: {path}")
            if is_placeholder(ws.read(path)[:4000]):
                raise RuntimeError(f"محتوای قالبی در فایل ذخیره شده: {path}")

    def _make_zip(self, ws, name):
        import zipfile
        skip = {".geta", "dist", "build", "__pycache__", "browser_profiles", "debug"}
        out = ws.root / f"{name}.zip"
        count = 0
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            for p in ws.root.rglob("*"):
                if not p.is_file() or p == out:
                    continue
                rel = p.relative_to(ws.root)
                if any(part in skip for part in rel.parts):
                    continue
                z.write(p, rel.as_posix())
                count += 1
        self.log(f"✓ فایل زیپ ساخته شد ({count} فایل): {out}")
        return out

    def _emit(self, info):
        if self.on_output:
            try:
                self.on_output(info)
            except Exception:
                pass

    def _emit_folder(self, title):
        if self._ws is not None:
            self._emit({"kind": "folder", "path": str(self._ws.root), "folder": str(self._ws.root), "title": title})

    def _save_reply(self, role, stage, prompt, reply):
        if self._ws is None:
            return
        try:
            d = self._ws.meta / "replies"
            d.mkdir(parents=True, exist_ok=True)
            seq = len(list(d.glob("*.md"))) + 1
            provider = getattr(getattr(self.ask, "__self__", None), "last_provider", None) or "?"
            slug = re.sub(r"[^\w\-]+", "_", stage)[:40].strip("_") or "step"
            path = d / f"{seq:03d}_{role}_{slug}.md"
            head = prompt[:1500] + ("\n…" if len(prompt) > 1500 else "")
            path.write_text(
                f"مرحله: {stage}\nنقش: {role} | سرویس: {provider} | زمان: {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                f"----- شروع پرامپت -----\n{head}\n\n----- پاسخ خام -----\n{reply}\n",
                encoding="utf-8")
            self._reply_path = path
            devlog.record_exchange(role=role, provider=provider, stage=stage, prompt=prompt, raw_reply=reply, cleaned_reply=reply)
        except OSError:
            self._reply_path = None

    def _note(self, text):
        if getattr(self, "_reply_path", None) is None:
            return
        try:
            with self._reply_path.open("a", encoding="utf-8") as f:
                f.write("\n----- استخراج‌شده -----\n" + text + "\n")
        except OSError:
            pass

    def _is_web(self, ws, task):
        if (ws.root / "index.html").is_file() or list(ws.root.glob("*.html")):
            return True
        low = (task or "").lower()
        words = ("html", "وب", "مرورگر", "browser", "web", "سایت", "website", "javascript", "جاوااسکریپت", "three.js")
        return any(w in low for w in words)

    def _deliver_web(self, ws, name, cp=None, brief=""):
        for attempt in range(2):
            try:
                bundle, warns = bundle_project(ws.root, name)
                for w in warns:
                    self.log(f"⚠ {w}")
                self.log(f"🎮 بازی/پروژه‌ی وب آماده است (فایل تکی): {bundle}")
                self._emit({"kind": "html", "path": str(bundle), "folder": str(ws.root), "title": "بازی آماده است"})
                return bundle
            except BundleError as e:
                if attempt == 0 and cp is not None:
                    self.log(f"⚠ خطا در بسته‌بندی وب ({e})؛ درخواست اصلاح از هماهنگ‌کننده…")
                    self._repair(ws, cp, brief, "html", str(e), 1)
                else:
                    self.log(f"⚠ ساخت HTML تکی ممکن نشد: {e}")
                    idx = ws.root / "index.html"
                    if idx.is_file():
                        self._emit({"kind": "html", "path": str(idx), "folder": str(ws.root), "title": "فایل index.html آماده است"})
                        return idx
        return None

    def _open_file(self, path):
        try:
            if os.name == "nt":
                os.startfile(str(path))
            else:
                webbrowser.open(f"file://{path}")
            self.log(f"▶ باز شد: {path}")
        except OSError as e:
            self.log(f"✗ خطا: باز کردن فایل ممکن نشد: {e}")

    def _paused(self, cp, err):
        devlog.error("Pipeline._paused", err if isinstance(err, BaseException) else None, str(err))
        self._emit_folder("کار متوقف شد")
        cp.pause(err)
        done, total = cp.counts()
        self.log(f"⏸ کار متوقف شد: {err}")
        self.log(f"⏸ {done} از {total} فایل انجام شده و وضعیت در .geta/state.json ذخیره شد.")
        self.log("برای ادامه، همین پوشه را انتخاب کن، حالت «ادامه کار نیمه‌تمام» را بزن و ساخت را بزن.")
        return False

    def _stopped(self, cp):
        self._emit_folder("متوقف شد")
        cp.pause("توسط کاربر متوقف شد")
        done, total = cp.counts()
        self.log(f"⏹ متوقف شد. {done} از {total} فایل انجام شده و وضعیت ذخیره شد.")
        self.log("هر وقت خواستی همین پوشه را با حالت «ادامه کار نیمه‌تمام» ادامه بده.")
        return False

    def _plan(self, ws, mode, task, name):
        tree = ws.tree_text()
        if mode == "edit":
            contents, complete = ws.snapshot()
            if not complete:
                sel = extract_json(self._call("architect", prompts.architect_select(task, tree),
                                              "انتخاب فایل‌ها"), "read") or {}
                paths = [p for p in sel.get("read", []) if isinstance(p, str) and ws.exists(p)][:25]
                contents, _ = ws.snapshot(paths, budget=60_000)
            prompt = prompts.architect_edit(task, tree, contents or "(no readable files)")
        else:
            prompt = prompts.architect_new(task, name, tree)

        for attempt in range(2):
            reply_text = self._call("architect", prompt, "برنامه‌ریزی")
            raw = extract_json(reply_text, "files")
            if raw:
                try:
                    plan_ok = self._normalize_plan(ws, raw, mode)
                    devlog.event("PLAN_PARSED", f"نقشه با موفقیت استخراج شد ({len(plan_ok['files'])} فایل)", plan_ok)
                    return plan_ok
                except ValueError as e:
                    devlog.error("Pipeline._normalize_plan", e, extra_data={"raw_extracted": raw, "reply_text": reply_text[:2000]})
                    self.log(f"⚠ نقشه نامعتبر بود: {e}")
            else:
                devlog.event("PLAN_JSON_FAIL", f"تلاش {attempt + 1}: از پاسخ معمار JSON استخراج نشد", reply_text[:2000])
            if attempt == 0:
                self.log("⚠ پاسخ معمار JSON استاندارد نبود؛ درخواست مجدد با فرمت فشرده…")
                prompt = (
                    f"Return ONLY a valid JSON object for building this project: {task}\n"
                    f"Format:\n"
                    f'{{"summary": "short description", "files": [{{"path": "index.html", "action": "create", "goal": "complete runnable app/game"}}], "run": "index.html"}}\n'
                    f"No markdown explanations, ONLY the raw JSON object starting with {{ and ending with }}."
                )

        # اگر بعد از ۲ بار باز هم مدل به جای JSON توضیح متنی داد، نقشه‌ی هوشمند پیش‌فرض می‌سازیم تا کار هرگز متوقف نشود!
        is_py = any(w in (task or "").lower() for w in ("python", "pygame", "tkinter", "pyqt", "پایتون", "exe"))
        default_file = "main.py" if is_py else "index.html"
        default_run = "python main.py" if is_py else "index.html"
        fallback_plan = {
            "summary": f"Complete {name or 'project'} implementation based on user request",
            "files": [{"path": default_file, "action": "create", "goal": f"Complete, self-contained runnable implementation of: {task[:300]}"}],
            "run": default_run,
        }
        self.log(f"ℹ نقشه به صورت هوشمند تنظیم شد ({default_file}) تا ساخت بدون توقف ادامه یابد.")
        devlog.event("PLAN_FALLBACK_USED", "استفاده از نقشه‌ی هوشمند جایگزین برای جلوگیری از توقف", fallback_plan)
        return fallback_plan

    def _normalize_plan(self, ws, raw, mode):
        files, seen = [], set()
        if is_placeholder(raw.get("summary")):
            raise ValueError("معمار متن قالب پرامپت را برگرداند، نه طرح واقعی پروژه")
        for f in raw.get("files") or []:
            if not isinstance(f, dict):
                continue
            if is_placeholder(f.get("path")) or is_placeholder(f.get("goal")):
                self.log(f"⚠ فایل قالبی رد شد: {f.get('path')}")
                continue
            try:
                rel = ws.rel(f.get("path", ""))
            except ValueError as e:
                self.log(f"⚠ رد شد: {e}")
                continue
            if rel in seen:
                continue
            if rel.endswith("__init__.py"):
                try:
                    ws.write(rel, "# package\n")
                except Exception:
                    pass
                continue
            seen.add(rel)
            exists = ws.exists(rel)
            action = str(f.get("action") or "").lower()
            if action not in ("create", "modify", "delete"):
                action = "modify" if exists else "create"
            if action == "modify" and not exists:
                action = "create"
            if action == "create" and exists and mode == "edit":
                action = "modify"
            if action == "delete" and not exists:
                continue
            files.append({"path": rel, "action": action,
                          "goal": str(f.get("goal") or f.get("description") or "")})
        if not files:
            raise ValueError("هیچ فایل معتبری در نقشه نبود")
        run = str(raw.get("run") or "")
        if is_placeholder(run):
            run = ""
        return {"summary": str(raw.get("summary", "")), "files": files, "run": run}

    def _written_context(self, ws, cp, exclude_path=""):
        """خلاصه‌ی هوشمند از فایل‌های قبلی (امضای کلاس‌ها، توابع، ثابت‌ها و ایمپورت‌ها) تا پرامپت بیش از حد طولانی نشود."""
        done_paths = [
            f["path"] for f in (cp.data.get("files") or [])
            if f.get("status") in ("done", "coded") and f.get("path") != exclude_path and ws.exists(f.get("path", ""))
        ]
        if not done_paths:
            return ""
        text, _ = ws.snapshot(paths=done_paths, budget=14_000, per_file=4_500)
        return text

    def _process_files(self, ws, cp, task):
        files = cp.data["files"]
        summary = (cp.data.get("plan") or {}).get("summary", "")
        for i, item in cp.pending():
            cancel.check()
            path = item["path"]
            if item["action"] == "delete":
                ws.remove(path)
                cp.mark(i, "done")
                self.log(f"🗑 حذف شد (بکاپ دارد): {path}")
                continue

            ctx_files = self._written_context(ws, cp, exclude_path=path)
            content = cp.load_draft(path) if item["status"] == "coded" else None
            if content is None:
                self._report(f"نوشتن {path}")
                current = ws.read(path) if item["action"] == "modify" and ws.exists(path) else None
                content = self._generate(task, summary, files, item, current, ctx_files)
                cp.save_draft(path, content)
                cp.mark(i, "coded")

            skip_heavy_single = (
                len(files) == 1 and path.lower().endswith((".html", ".htm"))
                and len(content) > 12_000 and not check_syntax(path, content)
            )
            if self.review and not skip_heavy_single:
                self._report(f"بازبینی {path}")
                rv_raw = self._call("reviewer",
                                    prompts.reviewer(task, files, item, content, ctx_files),
                                    f"بازبینی {path}")
                fixed = parse_review(rv_raw)
                if fixed is None and reviewer_wants_rewrite(rv_raw):
                    self.log(f"⚠ بازبین گفت {path} کامل نیست؛ فایل دوباره نوشته می‌شود.")
                    cur = ws.read(path) if item["action"] == "modify" and ws.exists(path) else None
                    content = self._generate(task, summary, files, item, cur, ctx_files, extra=rv_raw[:2000])
                    cp.save_draft(path, content)
                    fixed = None
                self._note("بازبین: " + ("اصلاح کرد" if fixed else "OK"))
                if fixed and not is_placeholder(fixed):
                    syn_err = check_syntax(path, fixed)
                    if not syn_err and len(fixed) >= len(content) * 0.55:
                        content = fixed
                        self.log(f"✎ بازبین {path} را اصلاح کرد")
                    else:
                        self.log(f"⚠ خروجی بازبین ناقص یا دارای خطا بود؛ نسخه‌ی اصلی کدنویس حفظ شد.")
            elif self.review and skip_heavy_single:
                self.log(f"ℹ فایل {path} کامل و سنگین است ({len(content)} کاراکتر)؛ بررسی نهایی با هماهنگ‌کننده انجام می‌شود.")

            ws.write(path, content)
            cp.mark(i, "done")
            cp.drop_draft(path)
            self.log(f"✓ {'ویرایش شد' if item['action'] == 'modify' else 'ساخته شد'}: {path}")
            self._report(f"{path} آماده شد")

    def _handoff_prefix(self):
        """خلاصه‌ی کار تا الان که اول گفتگوی تازه می‌ره تا مدل بدونه داره چیکار می‌کنه."""
        cp = getattr(self, "_cp", None)
        files = [f.get("path", "") for f in (cp.data.get("files") or [])] if cp is not None else []
        return ("[ادامه‌ی یک پروژه: گفتگوی قبلی به سقف رسیده بود و این گفتگوی تازه است. خلاصه‌ی کار تا الان:]\n"
                f"{(getattr(self, '_brief', '') or '')[:3500]}\n\n"
                f"فایل‌های پروژه: {', '.join(f for f in files if f) or 'هنوز نیست'}\n\n"
                f"امضاهای فایل‌های نوشته‌شده:\n{(getattr(self, '_last_ctx', '') or '')[:6000]}\n"
                "[پایان خلاصه — حالا درخواست زیر را انجام بده]\n\n")

    def _generate(self, task, summary, files, item, current, ctx_files="", extra=""):
        if ctx_files:
            self._last_ctx = ctx_files
        prompt = prompts.coder(task, summary, files, item, current, ctx_files)
        if extra:
            prompt += ("\n\nREVIEWER FEEDBACK - the previous version was incomplete or wrong. "
                       "Fix all of it and return the COMPLETE file:\n" + extra)
        for attempt in range(2):
            avail = list(getattr(getattr(self.ask, "__self__", None), "available", []) or [])
            last = getattr(getattr(self.ask, "__self__", None), "last_provider", None)
            pref = prefer_for_file(item["path"], avail, last) if avail else None
            raw = self._call("coder", prompt, f"نوشتن {item['path']}", prefer=pref)
            content = extract_file(raw)
            if is_placeholder(content) or is_placeholder_content(content):
                self.log(f"⚠ کدنویس متن قالب را برای {item['path']} برگرداند؛ تلاش دوباره")
                continue
            if content.strip():
                syn_err = check_syntax(item["path"], content)
                if syn_err and attempt == 0:
                    self.log(f"⚠ {syn_err} در {item['path']}؛ درخواست اصلاح از کدنویس…")
                    prompt += f"\n\nIMPORTANT: Your previous code had an error ({syn_err}). Return the COMPLETE, valid file."
                    continue
                self._note(f"فایل: {item['path']} | فرمت اعلام‌شده: {declared_format(raw)} | خطوط: {content.count(chr(10))}")
                return content
            self.log(f"⚠ پاسخ خالی برای {item['path']}؛ تلاش دوباره")
        raise RuntimeError(f"کدنویس برای {item['path']} محتوایی نداد")

    def _call(self, role, prompt, stage, prefer=None):
        resumes = 0
        read_retries = 0
        full_recoveries = 0
        while True:
            cancel.check()
            t0 = time.time()
            try:
                raw = str(self.ask(role, prompt, prefer=prefer) if prefer else self.ask(role, prompt))
                reply = strip_echo(raw, prompt)
                if raw.strip() and not reply.strip():
                    self._save_reply(role, stage + " (فقط پرامپت خوانده شد)", prompt, raw)
                    raise ReadFailed("فقط متن پرامپت خوانده شد، نه پاسخ")
                if reply != raw:
                    self.log("ℹ متن پرامپت از پاسخ هوش مصنوعی جدا شد")
                self._save_reply(role, stage, prompt, reply)
                if not reply.strip():
                    raise RuntimeError("پاسخ خالی")
                self._durations.append(time.time() - t0)
                return reply
            except NeedsLogin:
                raise
            except ConversationFull:
                # گفتگو پر شده؛ همون درخواست را در گفتگوی تازه با خلاصه‌ی پروژه می‌فرستیم
                if full_recoveries >= 2:
                    raise BuildPaused(f"{stage}: گفتگوی سرویس چند بار پر شد")
                full_recoveries += 1
                self.log(f"ℹ گفتگوی سرویس پر شده بود؛ گفتگوی تازه با خلاصه‌ی پروژه شروع شد ({full_recoveries}/2)")
                prompt = self._handoff_prefix() + prompt
            except ReadFailed as e:
                if read_retries >= 3:
                    raise BuildPaused(f"{stage}: {e}") from e
                read_retries += 1
                self.log(f"⚠ {e}. ۲۰ ثانیه صبر و خواندن دوباره ({read_retries}/3)")
                self._sleep(20)
            except Exception as e:
                if resumes >= self.max_auto_resumes:
                    raise BuildPaused(f"{stage}: {e}") from e
                resumes += 1
                mins = max(1, self.auto_resume_wait // 60)
                self.log(f"⏸ همه‌ی ایجنت‌ها فعلاً در دسترس نیستند ({e}). "
                         f"{mins} دقیقه صبر و تلاش دوباره ({resumes}/{self.max_auto_resumes})")
                self._report(f"در انتظار رفع محدودیت · {stage}")
                self._sleep(self.auto_resume_wait)

    def _refine(self, task, cp, mode):
        if cp.data.get("spec") is not None and cp.data.get("spec_for") == task:
            return cp.data.get("spec") or ""
        spec = ""
        if REFINE_WITH_AI and mode == "new" and len((task or "").strip()) < 600:
            spec = self._call("orchestrator", prompts.refiner(task), "تکمیل توضیح پروژه").strip()[:6000]
            self.log("📝 توضیح تکمیل‌شده: " + spec[:200].replace("\n", " ") + ("…" if len(spec) > 200 else ""))
        cp.data["spec"] = spec
        cp.data["spec_for"] = task
        cp.save()
        return spec

    def _sleep(self, seconds):
        end = time.time() + seconds
        while (left := end - time.time()) > 0:
            if int(left) % 60 == 0 and int(left) > 0:
                self.log(f"… {int(left) // 60} دقیقه مانده تا تلاش دوباره")
            cancel.sleep(min(1, left))

    def _report(self, stage):
        files = (self.cp.data.get("files") if self.cp else None) or []
        per = 2 if self.review else 1
        total = 1 + (len(files) if files else 6) * per
        done = 1 if files else 0
        for f in files:
            if f["status"] == "done":
                done += per
            elif f["status"] == "coded" and self.review:
                done += 1
        pct = min(99, round(done * 100 / total))
        recent = self._durations[-8:]
        eta = int(sum(recent) / len(recent) * (total - done)) if recent else None
        self.progress(pct, eta, stage)

    def _entry_from_run(self, ws, run_cmd):
        m = re.search(r"([\w./\\-]+\.pyw?)", run_cmd or "")
        if m and ws.exists(m.group(1)):
            return ws.rel(m.group(1))
        for cand in ("main.py", "app.py", "game.py", "run.py"):
            if ws.exists(cand):
                return cand
        py_files = [f for f in ws.list_files() if f.endswith(".py")]
        return py_files[0] if py_files else None

    def _python(self):
        if getattr(sys, "frozen", False):
            return shutil.which("python") or shutil.which("py")
        return sys.executable

    def _install_requirements(self, ws):
        req = ws.root / "requirements.txt"
        py = self._python()
        if not req.is_file() or not py:
            return
        content = req.read_text(encoding="utf-8", errors="replace").strip()
        if not content:
            return
        self.log("📦 نصب پکیج‌های requirements.txt ...")
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            subprocess.run([py, "-m", "pip", "install", "-q", "-r", str(req)],
                           cwd=ws.root, timeout=600, creationflags=flags)
        except Exception as e:
            self.log(f"⚠ نصب requirements.txt کامل نشد: {e}")

    def _final_gate(self, ws, cp):
        bad = []
        for f in cp.data.get("files") or []:
            p = f.get("path", "")
            if not p or f.get("action") == "delete":
                continue
            if not ws.exists(p):
                bad.append(p)
                continue
            text = ws.read(p)
            if not text.strip() or is_placeholder_content(text) or is_placeholder(text[:4000]):
                bad.append(p)
                continue
            if check_syntax(p, text):
                bad.append(p)
                continue
            if p.lower().endswith((".html", ".htm")) and checks.html_issues(text):
                bad.append(p)
            elif p.lower().endswith(".js") and checks.js_balance_issue(text):
                bad.append(p)
        return bad

    def _import_errors(self, ws, py_files):
        out = []
        env = dict(os.environ, SDL_VIDEODRIVER="dummy", SDL_AUDIODRIVER="dummy")
        for f in py_files:
            norm = f.replace("\\", "/")
            if not norm.endswith(".py") or norm.endswith("main.py") or norm.startswith("tests/"):
                continue
            mod = norm[:-3].replace("/", ".")
            try:
                r = subprocess.run([sys.executable, "-c", f"import importlib; importlib.import_module({mod!r})"],
                                   cwd=str(ws.root), capture_output=True, text=True, timeout=40, env=env,
                                   encoding="utf-8", errors="replace")
            except subprocess.TimeoutExpired:
                continue
            if r.returncode != 0:
                tail = (r.stderr or "").strip().splitlines()[-3:]
                out.append(f"{f}: خطای import — " + " | ".join(tail))
        return out

    def _test_python_entry(self, ws, cp, brief, fmt):
        py_files = [f for f in ws.list_files() if f.endswith((".py", ".pyw"))]
        if not py_files:
            return
        errors = []
        for f in py_files:
            err = check_syntax(f, ws.read(f))
            if err:
                errors.append(f"{f}: {err}")
        if not errors:
            errors = self._import_errors(ws, py_files)
        if errors:
            self.log("⚠ خطای سینتکس یا import در فایل‌های پایتون دیده شد؛ در حال تعمیر خودکار…")
            self._repair(ws, cp, brief, fmt, "\n".join(errors), 1)

    def _build_exe(self, ws, run_cmd, name, entry_hint=None):
        entry = entry_hint if entry_hint and ws.exists(entry_hint) else self._entry_from_run(ws, run_cmd)
        py = self._python()
        if not entry or not py:
            msg = "فایل اصلی پایتون یا مفسر پایتون برای ساخت exe پیدا نشد."
            self.log("✗ خطا: " + msg)
            return False, msg, None
        self.log(f"⚙ ساخت exe از {entry} ...")
        self.progress(99, None, "ساخت exe")
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            subprocess.run([py, "-c", "import PyInstaller"], cwd=ws.root, check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)
        except Exception:
            self.log("📦 در حال نصب PyInstaller (فقط بار اول)…")
            subprocess.run([py, "-m", "pip", "install", "-q", "pyinstaller"], cwd=ws.root,
                           timeout=600, creationflags=flags)

        safe_name = re.sub(r"[^\w\-]+", "_", name or ws.root.name).strip("_") or "app"
        cmd = [py, "-m", "PyInstaller", "--onefile", "--noconfirm", "--name", safe_name, entry]
        lines = []
        try:
            proc = subprocess.Popen(cmd, cwd=ws.root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, encoding="utf-8", errors="replace", creationflags=flags)
            for line in proc.stdout:
                if line.strip():
                    lines.append(line.rstrip())
                    self.log("  " + line.rstrip())
            if proc.wait() == 0:
                dist_exe = ws.root / "dist" / f"{safe_name}.exe"
                root_exe = ws.root / f"{safe_name}.exe"
                if dist_exe.is_file():
                    try:
                        shutil.copy2(dist_exe, root_exe)
                    except OSError:
                        root_exe = dist_exe
                final_exe = root_exe if root_exe.is_file() else dist_exe
                self.log(f"✓ فایل exe آماده شد: {final_exe}")
                return True, "", final_exe
            self.log("✗ خطا: ساخت exe ناموفق بود.")
            return False, "\n".join(lines[-80:]), None
        except OSError as e:
            self.log(f"✗ خطا: اجرای PyInstaller ممکن نشد: {e}")
            return False, str(e), None

    def _files_text(self, ws, cp):
        """فهرست همه‌ی فایل‌ها (کامل) + متن فایل‌ها تا حد مجاز. قبلاً متن بعد از سقف حذف می‌شد و هماهنگ‌کننده فایل‌ها رو گمشده می‌دید."""
        paths = [f.get("path", "") for f in (cp.data.get("files") or []) if f.get("path")]
        man = checks.manifest(ws.root, paths)
        text, complete = ws.snapshot(paths=paths, budget=60_000, per_file=22_000)
        note = "" if complete else "\n(بعضی فایل‌ها فقط در فهرست بالا آمده‌اند؛ همه‌ی فایل‌ها وجود دارند.)"
        return "فهرست فایل‌ها:\n" + man + "\n\nمتن فایل‌ها:\n" + text + note

    def _resolve_static(self, ws, cp, brief):
        """import های محلی گمشده را پیدا و فایل/نام‌های لازم را می‌سازد. برمی‌گرداند: مشکل‌های باقی‌مانده."""
        left = []
        for round_no in range(1, 3):
            issues = checks.find_issues(ws.root)
            if not issues:
                return []
            needs = {}
            for it in issues:
                path = it["module"].replace(".", "/") + ".py"
                info = needs.setdefault(path, {"names": set(), "users": set()})
                info["names"].update(it.get("names") or ([it["name"]] if it.get("name") else []))
                info["users"].add(it["file"])
            self.log(f"🔎 بررسی import (دور {round_no}): {len(issues)} مشکل؛ {len(needs)} فایل لازم است.")
            for path, info in needs.items():
                goal = ("Make sure this module exists and defines these exact names: "
                        + (", ".join(sorted(info["names"])) or "(module content)")
                        + ". Used by: " + ", ".join(sorted(info["users"]))
                        + ". Keep any existing content that is correct.")
                action = "modify" if ws.exists(path) else "create"
                item = next((f for f in cp.data.get("files", []) if f.get("path") == path), None)
                if item is None:
                    cp.data.setdefault("files", []).append({"path": path, "action": action, "goal": goal, "status": "pending"})
                else:
                    item.update({"action": action, "goal": goal, "status": "pending"})
            cp.save()
            self._process_files(ws, cp, brief)
            left = checks.find_issues(ws.root)
        return left

    def _apply_fixes(self, ws, cp, data):
        done = 0
        for fx in (data.get("fixes") or []):
            path = str(fx.get("path", "")).strip()
            content = fx.get("content")
            if not path or not isinstance(content, str) or is_placeholder(path) or is_placeholder(content[:4000]):
                continue
            if is_placeholder_content(content):
                self.log(f"⚠ اصلاح {path} فقط متن قالبی بود و رد شد.")
                continue
            if ws.exists(path):
                old = ws.read(path)
                if len(old) > 2000 and len(content) < len(old) * 0.6:
                    self.log(f"⚠ اصلاح {path} خیلی کوتاه‌تر از فایل فعلی بود ({len(content)} از {len(old)}) و رد شد.")
                    continue
            if check_syntax(path, content):
                self.log(f"⚠ اصلاح پیشنهادی برای {path} خطای سینتکس داشت و نادیده گرفته شد.")
                continue
            try:
                ws.write(path, content)
            except (OSError, ValueError) as e:
                self.log(f"⚠ اصلاح {path} ذخیره نشد: {e}")
                continue
            files = cp.data.setdefault("files", [])
            if not any(f.get("path") == path for f in files):
                files.append({"path": path, "action": "modify", "goal": "اصلاح توسط هماهنگ‌کننده", "status": "done"})
            self.log(f"🛠 اصلاح شد: {path}")
            done += 1
        cp.save()
        return done

    def _integrate(self, ws, cp, brief, fmt):
        files = cp.data.get("files") or []
        if not files:
            return
        if len(files) == 1 and files[0].get("path", "").lower().endswith((".html", ".htm")):
            p = files[0]["path"]
            if ws.exists(p) and not check_syntax(p, ws.read(p)):
                cp.data["entry"] = p
                cp.data["build_kind"] = "html"
                cp.save()
                self.log("🧭 هماهنگ‌کننده: فایل وب تکی سالم و آمادهٔ بسته‌بندی است.")
                return

        self._report("یکپارچه‌سازی کدها")
        try:
            prompt = prompts.orchestrator_integrate(brief, fmt, self._files_text(ws, cp), cp.data.get("entry"))
            data = extract_json(self._call("orchestrator", prompt, "یکپارچه‌سازی نهایی"), "entry")
        except Exception as e:
            self.log(f"⚠ یکپارچه‌سازی انجام نشد ({e}). بیلد با کدهای فعلی ادامه می‌یابد.")
            return
        if not data:
            self.log("⚠ پاسخ یکپارچه‌سازی معتبر نبود؛ ادامه با کدهای فعلی.")
            return
        entry = str(data.get("entry") or "").strip()
        if entry and ws.exists(entry):
            cp.data["entry"] = entry
        build = str(data.get("build") or "").strip().lower()
        cp.data["build_kind"] = build if build in ("html", "exe", "zip") else None
        if data.get("notes"):
            self.log("🧭 هماهنگ‌کننده: " + str(data["notes"])[:400])
        self._apply_fixes(ws, cp, data)

    def _repair(self, ws, cp, brief, fmt, build_log, attempt):
        self.log(f"🔧 هماهنگ‌کننده خطای بیلد را بررسی می‌کنه ({attempt}/2)")
        try:
            prompt = prompts.orchestrator_repair(brief, fmt, self._files_text(ws, cp), build_log, cp.data.get("entry"))
            data = extract_json(self._call("orchestrator", prompt, f"اصلاح بیلد ({attempt})"), "fixes")
        except Exception as e:
            self.log(f"⚠ اصلاح خودکار انجام نشد: {e}")
            return
        if data:
            self._apply_fixes(ws, cp, data)

    def _run_project(self, ws, run_cmd, entry_hint=None):
        entry = entry_hint if entry_hint and ws.exists(entry_hint) else self._entry_from_run(ws, run_cmd)
        if entry and entry.lower().endswith((".html", ".htm")):
            self._open_file(ws.resolve(entry))
            return
        if not run_cmd and entry:
            run_cmd = f'"{self._python()}" "{entry}"'
        elif run_cmd and run_cmd.startswith("python "):
            run_cmd = f'"{self._python()}" ' + run_cmd[len("python "):]
        if not run_cmd:
            self.log("⚠ دستور اجرا مشخص نیست.")
            return
        self.log(f"▶ اجرا: {run_cmd}")
        flags = subprocess.CREATE_NEW_CONSOLE if os.name == "nt" else 0
        try:
            subprocess.Popen(run_cmd, cwd=ws.root, shell=True, creationflags=flags)
        except OSError as e:
            self.log(f"✗ خطا: اجرا ناموفق بود: {e}")
