# -*- coding: utf-8 -*-
"""
main.py — نقطه ورود اندروید و دسکتاپ برای Geta Code
- اجرای UI اصلی داخل WebView تمام‌صفحه روی حالت افقی (Landscape)
- مدیریت دکمه بازگشت اندروید (بستن پنجره لاگین سرویس AI بدون بستن اپ)
- ارتباط امن لوکال بین UI و هسته گتا کد بدون هیچ‌گونه اتصال به Chrome اصلی گوشی
"""
import json
import os
import socket
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import geta_code as core
import cancel
import devlog
try:
    import power  # کنترل بیدار ماندن صفحه (فقط ویندوز)
except Exception:
    power = None
from checkpoint import Checkpoint
from pipeline import Pipeline
from workspace import Workspace

ROOT = Path(__file__).resolve().parent
UI_FILE = ROOT / "ui.html"

IS_ANDROID = False
try:
    from kivy.app import App
    from kivy.uix.widget import Widget
    from kivy.clock import Clock
    from kivy.core.window import Window
    from jnius import autoclass, PythonJavaClass, java_method
    from android.runnable import run_on_ui_thread
    IS_ANDROID = True
except Exception:
    pass


class AndroidApi:
    """بک‌اند پایتون با پشتیبانی هم‌زمان از اندروید و دسکتاپ."""

    def __init__(self):
        self._lock = threading.Lock()
        self._busy = False
        self._last_stage = None
        self.ready = set()
        self._events = []
        self._seq = 0
        self._ev_lock = threading.Lock()
        self._active_login_page = None

    def _js(self, code: str):
        with self._ev_lock:
            self._seq += 1
            self._events.append({"seq": self._seq, "code": code})
            if len(self._events) > 500:
                self._events = self._events[-500:]

    def pop_events(self, since: int):
        with self._ev_lock:
            return [e for e in self._events if e["seq"] > since]

    def _log(self, message):
        self._js(f"appendLog({json.dumps(str(message), ensure_ascii=False)})")

    def _progress(self, pct, eta, stage):
        if stage != self._last_stage:
            self._last_stage = stage
            self._log(f"▸ {stage} ({pct}%)")

    def open_dev(self):
        dev_path = devlog.ensure_dev()
        self._log(f"📂 مسیر پوشه لاگ و دیباگ: {dev_path}")
        if not IS_ANDROID:
            if os.name == "nt":
                os.startfile(str(dev_path))
            else:
                webbrowser.open(dev_path.as_uri())
        return {"ok": True, "path": str(dev_path)}

    def cancel_login(self):
        cancel.STOP.set()
        if self._active_login_page is not None:
            try:
                self._active_login_page.set_overlay_mode(False)
            except Exception:
                pass
        return {"ok": True}

    def login(self, provider):
        devlog.event("LOGIN_START", f"درخواست ورود ایزوله به سرویس: {provider}")
        if provider not in core.PROVIDERS:
            return {"ok": False, "error": f"سرویس ناشناخته: {provider}"}
        cancel.STOP.clear()
        chat = None
        ok = False
        try:
            core.launch_browser(provider)
            chat = core.Chat(provider, None)
            if IS_ANDROID:
                self._log(f"🔐 صفحه ورود ایزوله {provider} باز شد (بدون اتصال به Chrome اصلی). پس از ورود، صفحه خودکار بسته می‌شود؛ یا دکمه Back گوشی را بزن.")
                page = chat._open()
                self._active_login_page = page
                if hasattr(page, "set_overlay_mode"):
                    page.set_overlay_mode(True)
                deadline = time.time() + 300
                while time.time() < deadline:
                    if cancel.STOP.is_set():
                        break
                    if chat._has_input(page) and not chat._login_visible(page):
                        ok = True
                        break
                    time.sleep(2.0)
            else:
                from playwright.sync_api import sync_playwright
                pw = sync_playwright().start()
                try:
                    chat = core.Chat(provider, pw)
                    ok = chat.wait_for_login(timeout=300)
                finally:
                    pw.stop()
        except Exception as error:
            devlog.error(f"Api.login({provider})", error)
            return {"ok": False, "error": str(error)}
        finally:
            if self._active_login_page is not None:
                try:
                    self._active_login_page.set_overlay_mode(False)
                except Exception:
                    pass
                self._active_login_page = None
            cancel.STOP.clear()
            if chat is not None:
                chat.close()
        if ok:
            self.ready.add(provider)
            return {"ok": True}
        return {"ok": False, "error": "ورود تأیید نشد. داخل پنجره ایزوله وارد حساب شو و دوباره امتحان کن."}

    def pick_folder(self):
        # در اندروید به صورت خودکار پوشه پروژه‌ها در حافظه داخلی ایجاد و انتخاب می‌شود
        base = core.PROJECTS
        base.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("project_%H%M%S")
        target = base / stamp
        target.mkdir(parents=True, exist_ok=True)
        return self.scan_folder(str(target))

    def scan_folder(self, folder):
        p = Path(folder)
        if not p.is_dir():
            return {"ok": False, "error": "پوشه پیدا نشد"}
        ws = Workspace(p)
        info = {"ok": True, "path": str(ws.root), "files": len(ws.list_files()), "checkpoint": None}
        cp = Checkpoint(ws)
        if cp.exists():
            try:
                d = cp.load()
                done, total = cp.counts()
                info["checkpoint"] = {
                    "status": d.get("status"), "task": d.get("task", ""),
                    "mode": d.get("mode"), "done": done, "total": total,
                    "error": d.get("error", "")
                }
            except Exception:
                pass
        return info

    def start_build(self, task, name, roles, make_exe, auto_run, folder=None, mode="new", keep_awake=True):
        if isinstance(roles, dict):
            role_map = {k: v for k, v in roles.items() if v and v != "auto"}
            selected = set(roles.keys())
        else:
            role_map = {}
            selected = set(roles or [])
        if not {"architect", "coder"} <= selected:
            return {"ok": False, "error": "نقش معمار و کدنویس باید انتخاب شوند."}
        available = [p for p in core.PROVIDERS if p in self.ready]
        if core.api_configured():
            available = core.configured_api_providers() + [p for p in available if p not in core.API_PROVIDERS]
        if not available:
            return {"ok": False, "error": "حداقل یک سرویس را وارد کن (دکمه‌های بالا)."}
        for role, provider in role_map.items():
            if provider not in available:
                return {"ok": False, "error": f"سرویس «{provider}» برای نقش «{role}» انتخاب شده ولی وصل نیست."}
        roles = sorted(selected)
        self._role_map = role_map
        with self._lock:
            if self._busy:
                return {"ok": False, "error": "یک ساخت در حال اجراست"}
            self._busy = True
        cancel.STOP.clear()
        self._last_stage = None
        threading.Thread(
            target=self._worker, daemon=True,
            args=(task, (name or "").strip(), list(selected), False,
                  bool(auto_run), folder, mode or "new", available, bool(keep_awake))
        ).start()
        return {"running": True}

    def stop_build(self):
        if not self._busy:
            return {"ok": False, "error": "ساختی در حال اجرا نیست"}
        cancel.STOP.set()
        self._log("⏹ درخواست توقف ثبت شد؛ کار فعلی ذخیره و متوقف می‌شود…")
        return {"ok": True}

    def open_path(self, path):
        p = Path(path)
        if not p.exists():
            return {"ok": False, "error": "فایل پیدا نشد"}
        self._log(f"📄 فایل خروجی در مسیر ذخیره شد: {p}")
        return {"ok": True}

    def reveal_path(self, path):
        p = Path(path)
        self._log(f"📁 پوشه خروجی: {p.parent if p.is_file() else p}")
        return {"ok": True}

    def list_replies(self, folder):
        d = Path(folder) / ".geta" / "replies"
        if not d.is_dir():
            return {"ok": True, "items": []}
        items = [{"name": p.name, "path": str(p), "size": p.stat().st_size} for p in sorted(d.glob("*.md"))]
        return {"ok": True, "items": items}

    def read_reply(self, path):
        p = Path(path)
        if not p.is_file() or p.parent.name != "replies" or p.parent.parent.name != ".geta":
            return {"ok": False, "error": "فایل پیدا نشد"}
        return {"ok": True, "text": p.read_text(encoding="utf-8", errors="replace")[:300000]}

    def _worker(self, task, name, roles, make_exe, auto_run, folder, mode, available, keep_awake=True):
        ok = False
        builder = None
        if keep_awake and power is not None:
            power.keep_awake(True)
        try:
            if not folder:
                folder = str(core.PROJECTS / (name or "project"))
            builder = core.Builder(
                dict(core.DEFAULT_ROLES, **core.auto_roles(available, roles, getattr(self, "_role_map", {}))),
                Path(folder), log=self._log, force_exe=False, allow_run=False, available=available
            )
            builder.auto_roles = {r for r in roles if r not in getattr(self, "_role_map", {})}
            pipe = Pipeline(
                builder.ask, self._log, self._progress,
                on_output=lambda info: self._js("window.showOutput && window.showOutput(" + json.dumps(info, ensure_ascii=False) + ")")
            )
            ok = pipe.run(folder, mode, task, name, roles, False, False)
        except cancel.BuildCancelled:
            self._log("⏹ متوقف شد.")
        except Exception as error:
            devlog.error("Api._worker", error)
            self._log(f"✗ خطا: {error}")
        finally:
            if builder is not None:
                builder.close()
            if power is not None:
                power.release()
            cancel.STOP.clear()
            with self._lock:
                self._busy = False
            self._js(f"finishProgress({'true' if ok else 'false'})")
            self._js("setBuilding(false)")
            if folder:
                self._js(f"window.wsAdopt && window.wsAdopt({json.dumps(self.scan_folder(folder), ensure_ascii=False)})")


def start_local_server(api: AndroidApi) -> int:
    html_bytes = UI_FILE.read_bytes()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def _send_json(self, obj, code=200):
            data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path in ("/", "/index.html"):
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(html_bytes)))
                self.end_headers()
                self.wfile.write(html_bytes)
            elif parsed.path == "/events":
                qs = parse_qs(parsed.query)
                since = int((qs.get("since") or ["0"])[0])
                self._send_json({"events": api.pop_events(since)})
            else:
                self.send_response(404)
                self.end_headers()

        def do_POST(self):
            parsed = urlparse(self.path)
            if not parsed.path.startswith("/api/"):
                self.send_response(404)
                self.end_headers()
                return
            method_name = parsed.path[len("/api/"):]
            fn = getattr(api, method_name, None)
            if fn is None or method_name.startswith("_"):
                self._send_json({"ok": False, "error": "unknown method"}, 404)
                return
            length = int(self.headers.get("Content-Length") or "0")
            raw = self.rfile.read(length) if length > 0 else b"{}"
            try:
                payload = json.loads(raw.decode("utf-8"))
                args = payload.get("args") or []
                res = fn(*args)
                self._send_json(res if res is not None else {"ok": True})
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, 500)

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return port


if IS_ANDROID:
    class GetaAndroidApp(App):
        def build(self):
            # اگه راه‌اندازی خطا بده، به جای صفحه‌ی سیاه، متن خطا نشون داده میشه
            try:
                self.api = AndroidApi()
                self.port = start_local_server(self.api)
                Window.bind(on_keyboard=self._on_key)
                Clock.schedule_once(lambda dt: self._mount_main_webview(), 0.2)
                return Widget()
            except Exception:
                import traceback
                from kivy.uix.label import Label
                return Label(text="خطا در راه‌اندازی Geta Code:\n" + traceback.format_exc()[-1500:],
                             font_size="13sp", halign="left", valign="top", text_size=(Window.width - 40, None))

        def _on_key(self, window, key, *args):
            # دکمه Back اندروید (key == 27): اگر پنجره لاگین سرویس باز است، فقط آن را ببند
            if key == 27:
                if self.api._active_login_page is not None:
                    self.api.cancel_login()
                    return True
                return True
            return False

        @run_on_ui_thread
        def _mount_main_webview(self):
            WebView = autoclass("android.webkit.WebView")
            WebViewClient = autoclass("android.webkit.WebViewClient")
            WebChromeClient = autoclass("android.webkit.WebChromeClient")
            LayoutParams = autoclass("android.widget.FrameLayout$LayoutParams")
            PythonActivity = autoclass("org.kivy.android.PythonActivity")
            ActivityInfo = autoclass("android.content.pm.ActivityInfo")
            View = autoclass("android.view.View")

            activity = PythonActivity.mActivity
            # قفل اجباری روی حالت افقی (Landscape)
            activity.setRequestedOrientation(ActivityInfo.SCREEN_ORIENTATION_SENSOR_LANDSCAPE)

            # تمام‌صفحه واقعی (Immersive Sticky) تا نوار وضعیت و ناوبری تصویر را بهم نریزد
            try:
                decor = activity.getWindow().getDecorView()
                decor.setSystemUiVisibility(
                    View.SYSTEM_UI_FLAG_LAYOUT_STABLE
                    | View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION
                    | View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN
                    | View.SYSTEM_UI_FLAG_HIDE_NAVIGATION
                    | View.SYSTEM_UI_FLAG_FULLSCREEN
                    | View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY
                )
            except Exception:
                pass

            wv = WebView(activity)
            s = wv.getSettings()
            s.setJavaScriptEnabled(True)
            s.setDomStorageEnabled(True)
            s.setDatabaseEnabled(True)
            s.setUseWideViewPort(True)
            s.setLoadWithOverviewMode(True)
            s.setTextZoom(100)
            wv.setWebViewClient(WebViewClient())
            wv.setWebChromeClient(WebChromeClient())

            root = activity.getWindow().getDecorView().findViewById(autoclass("android.R$id").content)
            root.addView(wv, LayoutParams(-1, -1))
            wv.loadUrl(f"http://127.0.0.1:{self.port}/")
            self._main_wv = wv


def main():
    if IS_ANDROID:
        GetaAndroidApp().run()
    else:
        import webview
        api = AndroidApi()
        port = start_local_server(api)
        webview.create_window(
            "Geta Code",
            url=f"http://127.0.0.1:{port}/",
            width=1280,
            height=820,
            min_size=(960, 640),
            background_color="#0a0607",
        )
        webview.start(debug=False)


if __name__ == "__main__":
    main()
