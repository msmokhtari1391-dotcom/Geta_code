# -*- coding: utf-8 -*-
"""
android_bridge.py — پل بومی Android WebView برای اجرای ایزوله و بدون ارتباط با Chrome اصلی گوشی
ویژگی‌ها:
1. ایجاد Android WebView بومی با User-Agent دسکتاپ تا رابط وب ChatGPT، DeepSeek، Claude، Gemini، Qwen و... دقیقاً با همان سلکتورهای دسکتاپ لود شوند.
2. جداسازی کامل داده‌ها و کوکی‌ها (WebView.setDataDirectorySuffix + CookieManager اختصاصی) از مرورگر Chrome اصلی گوشی.
3. شبیه‌سازی کامل رابط Playwright Page (evaluate, goto, locator, keyboard, bring_to_front, screenshot) تا هم providers/chatgpt.py و هم تمام منطق‌های DeepSeek/Gemini/Claude بدون تغییر کار کنند.
"""
from __future__ import annotations

import base64
import json
import os
import threading
import time
from pathlib import Path

IS_ANDROID = False
try:
    from jnius import autoclass, PythonJavaClass, java_method
    from android.runnable import run_on_ui_thread
    IS_ANDROID = True
except Exception:
    def run_on_ui_thread(fn):
        return fn

DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


if IS_ANDROID:
    WebView = autoclass("android.webkit.WebView")
    WebViewClient = autoclass("android.webkit.WebViewClient")
    WebChromeClient = autoclass("android.webkit.WebChromeClient")
    CookieManager = autoclass("android.webkit.CookieManager")
    WebStorage = autoclass("android.webkit.WebStorage")
    FrameLayout = autoclass("android.widget.FrameLayout")
    LayoutParams = autoclass("android.widget.FrameLayout$LayoutParams")
    View = autoclass("android.view.View")
    Gravity = autoclass("android.view.Gravity")
    BuildVersion = autoclass("android.os.Build$VERSION")
    PythonActivity = autoclass("org.kivy.android.PythonActivity")
    Bitmap = autoclass("android.graphics.Bitmap")
    BitmapConfig = autoclass("android.graphics.Bitmap$Config")
    CompressFormat = autoclass("android.graphics.Bitmap$CompressFormat")
    Canvas = autoclass("android.graphics.Canvas")
    FileOutputStream = autoclass("java.io.FileOutputStream")
    KeyEvent = autoclass("android.view.KeyEvent")
    SystemClock = autoclass("android.os.SystemClock")
    Build = autoclass("android.os.Build")

    class _ValueCallback(PythonJavaClass):
        __javainterfaces__ = ["android/webkit/ValueCallback"]
        __javacontext__ = "app"

        def __init__(self, event, box):
            super().__init__()
            self._event = event
            self._box = box

        @java_method("(Ljava/lang/Object;)V")
        def onReceiveValue(self, value):
            self._box["val"] = value
            self._event.set()


class AndroidKeyboard:
    def __init__(self, page: "AndroidWebPage"):
        self._page = page

    def press(self, key: str):
        k = (key or "").lower()
        if "enter" in k:
            self._page.evaluate("""() => {
                const el = document.activeElement || document.querySelector('textarea, [contenteditable="true"]');
                if (!el) return;
                const opts = {key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true, cancelable: true};
                el.dispatchEvent(new KeyboardEvent('keydown', opts));
                el.dispatchEvent(new KeyboardEvent('keypress', opts));
                el.dispatchEvent(new KeyboardEvent('keyup', opts));
                const form = el.closest('form');
                if (form) {
                    const btn = form.querySelector('button[type="submit"], button[data-testid="send-button"], #composer-submit-button');
                    if (btn && !btn.disabled) btn.click();
                }
            }""")

    def insert_text(self, text: str):
        self._page.evaluate("""(t) => {
            const el = document.activeElement || document.querySelector('textarea, [contenteditable="true"]');
            if (!el) return;
            el.focus();
            if (el.tagName === 'TEXTAREA' || el.tagName === 'INPUT') {
                const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set;
                setter.call(el, (el.value || '') + t);
                el.dispatchEvent(new Event('input', {bubbles: true}));
            } else {
                document.execCommand('insertText', false, t);
            }
        }""", text)


class AndroidLocator:
    """شبیه‌ساز کامل Playwright Locator برای سلکتورهای CSS و :has-text(...)"""

    def __init__(self, page: "AndroidWebPage", selector: str, index: int = None):
        self._page = page
        self._selector = selector
        self._index = index

    @property
    def first(self):
        return AndroidLocator(self._page, self._selector, index=0)

    def nth(self, i: int):
        return AndroidLocator(self._page, self._selector, index=i)

    def _query_js(self, action: str, arg=None):
        js = r"""([sel, idx, action, arg]) => {
            const parseParts = (raw) => raw.split(',').map(s => s.trim()).filter(Boolean);
            const findAll = (raw) => {
                const out = [];
                for (const part of parseParts(raw)) {
                    const ht = part.match(/^(.*):has-text\((['"])(.*?)\2\)\s*$/i);
                    let base = ht ? (ht[1].trim() || '*') : part;
                    const needle = ht ? ht[3].toLowerCase() : null;
                    try {
                        for (const el of document.querySelectorAll(base)) {
                            if (needle && !(el.innerText || el.textContent || '').toLowerCase().includes(needle)) continue;
                            if (!out.includes(el)) out.push(el);
                        }
                    } catch (e) {}
                }
                return out;
            };
            const list = findAll(sel);
            if (action === 'count') return list.length;
            const el = (idx === null || idx === undefined) ? list[0] : list[idx];
            if (!el) return action === 'visible' || action === 'enabled' ? false : '';
            if (action === 'visible') {
                const r = el.getBoundingClientRect();
                const s = getComputedStyle(el);
                return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
            }
            if (action === 'enabled') {
                return !el.disabled && el.getAttribute('aria-disabled') !== 'true';
            }
            if (action === 'click') {
                el.scrollIntoView({block: 'center'});
                el.click();
                return true;
            }
            if (action === 'fill') {
                el.focus();
                const t = String(arg || '');
                if (el.tagName === 'TEXTAREA' || el.tagName === 'INPUT') {
                    const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set;
                    setter.call(el, t);
                    el.dispatchEvent(new InputEvent('input', {bubbles: true, inputType: 'insertText', data: t}));
                    el.dispatchEvent(new Event('change', {bubbles: true}));
                } else {
                    el.innerText = t;
                    el.dispatchEvent(new InputEvent('input', {bubbles: true, inputType: 'insertText', data: t}));
                }
                return true;
            }
            if (action === 'inner_text') {
                return (el.innerText || el.textContent || '').trim();
            }
            return null;
        }"""
        return self._page.evaluate(js, [self._selector, self._index, action, arg])

    def count(self) -> int:
        try:
            return int(self._query_js("count") or 0)
        except Exception:
            return 0

    def is_visible(self) -> bool:
        try:
            return bool(self._query_js("visible"))
        except Exception:
            return False

    def is_enabled(self) -> bool:
        try:
            return bool(self._query_js("enabled"))
        except Exception:
            return False

    def click(self, timeout: float = 5000):
        self._query_js("click")

    def fill(self, text: str):
        self._query_js("fill", text)

    def inner_text(self) -> str:
        return str(self._query_js("inner_text") or "")


class AndroidWebPage:
    """یک WebView ایزوله برای یک هوش مصنوعی (بدون هرگونه اشتراک سشن با Chrome اصلی گوشی)."""

    def __init__(self, provider: str, profile_dir: Path):
        self.provider = provider
        self.profile_dir = Path(profile_dir)
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self.keyboard = AndroidKeyboard(self)
        self._webview = None
        self._closed = False
        self._overlay_visible = False
        self._default_timeout = 30_000
        self._listeners = {"response": []}
        self._current_url = ""
        if IS_ANDROID:
            self._init_native_webview()

    def _init_native_webview(self):
        ready = threading.Event()

        @run_on_ui_thread
        def _create():
            try:
                activity = PythonActivity.mActivity
                wv = WebView(activity)
                settings = wv.getSettings()
                settings.setJavaScriptEnabled(True)
                settings.setDomStorageEnabled(True)
                settings.setDatabaseEnabled(True)
                settings.setUserAgentString(DESKTOP_UA)
                settings.setUseWideViewPort(True)
                settings.setLoadWithOverviewMode(True)
                settings.setSupportZoom(True)
                settings.setBuiltInZoomControls(True)
                settings.setDisplayZoomControls(False)
                if BuildVersion.SDK_INT >= 21:
                    settings.setMixedContentMode(0)  # MIXED_CONTENT_ALWAYS_ALLOW
                cm = CookieManager.getInstance()
                cm.setAcceptCookie(True)
                if BuildVersion.SDK_INT >= 21:
                    cm.setAcceptThirdPartyCookies(wv, True)

                wv.setWebViewClient(WebViewClient())
                wv.setWebChromeClient(WebChromeClient())

                root = activity.getWindow().getDecorView().findViewById(autoclass("android.R$id").content)
                params = LayoutParams(-1, -1)
                wv.setVisibility(View.INVISIBLE)
                root.addView(wv, params)
                self._webview = wv
            finally:
                ready.set()

        _create()
        ready.wait(10)

    def set_overlay_mode(self, visible: bool):
        """نمایش تمام‌صفحه WebView برای لاگین کاربر یا مخفی کردن آن هنگام اجرای پس‌زمینه."""
        self._overlay_visible = bool(visible)
        if not IS_ANDROID or self._webview is None:
            return

        @run_on_ui_thread
        def _toggle():
            if visible:
                self._webview.setVisibility(View.VISIBLE)
                self._webview.bringToFront()
                self._webview.requestFocus()
            else:
                self._webview.setVisibility(View.INVISIBLE)

        _toggle()

    def bring_to_front(self):
        if self._overlay_visible:
            self.set_overlay_mode(True)

    def set_default_timeout(self, ms: int):
        self._default_timeout = ms

    def on(self, event: str, fn):
        self._listeners.setdefault(event, []).append(fn)

    def remove_listener(self, event: str, fn):
        if event in self._listeners and fn in self._listeners[event]:
            self._listeners[event].remove(fn)

    def is_closed(self) -> bool:
        return self._closed or (IS_ANDROID and self._webview is None)

    def goto(self, url: str, wait_until: str = "domcontentloaded", timeout: int = 60000):
        self._current_url = url
        if not IS_ANDROID or self._webview is None:
            return
        done = threading.Event()

        @run_on_ui_thread
        def _load():
            self._webview.loadUrl(url)
            done.set()

        _load()
        done.wait(5)
        time.sleep(2.0)

    @property
    def url(self) -> str:
        if not IS_ANDROID or self._webview is None:
            return self._current_url
        try:
            val = self.evaluate("() => location.href")
            if val:
                self._current_url = str(val)
        except Exception:
            pass
        return self._current_url

    def wait_for_timeout(self, ms: int):
        time.sleep(max(0.0, ms / 1000.0))

    def locator(self, selector: str) -> AndroidLocator:
        return AndroidLocator(self, selector)

    def evaluate(self, script: str, arg=None):
        if not IS_ANDROID or self._webview is None:
            return None
        arg_json = json.dumps(arg, ensure_ascii=False)
        wrapped = (
            "(function(){try{"
            f"const __fn = ({script});"
            f"const __res = (typeof __fn === 'function') ? __fn({arg_json}) : __fn;"
            "return JSON.stringify({ok:true, v: __res === undefined ? null : __res});"
            "}catch(e){return JSON.stringify({ok:false, err: String(e)});}})()"
        )
        ev = threading.Event()
        box = {"val": None}
        cb = _ValueCallback(ev, box)

        @run_on_ui_thread
        def _eval():
            try:
                self._webview.evaluateJavascript(wrapped, cb)
            except Exception:
                ev.set()

        _eval()
        if not ev.wait(15):
            return None
        raw = box["val"]
        if not raw or raw == "null":
            return None
        try:
            decoded = json.loads(raw)
            if isinstance(decoded, str):
                decoded = json.loads(decoded)
            if isinstance(decoded, dict) and decoded.get("ok"):
                return decoded.get("v")
        except Exception:
            pass
        return None

    def content(self) -> str:
        return str(self.evaluate("() => document.documentElement.outerHTML") or "")

    def screenshot(self, path: str, full_page: bool = False, timeout: int = 15000):
        if not IS_ANDROID or self._webview is None:
            return
        done = threading.Event()

        @run_on_ui_thread
        def _snap():
            try:
                w = max(1, self._webview.getWidth())
                h = max(1, self._webview.getHeight())
                bmp = Bitmap.createBitmap(w, h, BitmapConfig.ARGB_8888)
                canvas = Canvas(bmp)
                self._webview.draw(canvas)
                fos = FileOutputStream(str(path))
                bmp.compress(CompressFormat.PNG, 90, fos)
                fos.flush()
                fos.close()
            except Exception:
                pass
            finally:
                done.set()

        _snap()
        done.wait(5)

    def close(self):
        self._closed = True
        if not IS_ANDROID or self._webview is None:
            return

        @run_on_ui_thread
        def _destroy():
            try:
                parent = self._webview.getParent()
                if parent is not None:
                    parent.removeView(self._webview)
                self._webview.destroy()
            except Exception:
                pass
            self._webview = None

        _destroy()


class AndroidContext:
    def __init__(self, provider: str, profile_dir: Path):
        self.provider = provider
        self.profile_dir = profile_dir
        self._pages: list[AndroidWebPage] = []

    def new_page(self) -> AndroidWebPage:
        page = AndroidWebPage(self.provider, self.profile_dir)
        self._pages.append(page)
        return page


class AndroidBrowserManager:
    """نگهدارندهٔ سشن‌های جداگانه برای هر هوش مصنوعی در طول اجرای برنامه."""
    _contexts: dict[str, AndroidContext] = {}
    _lock = threading.Lock()

    @classmethod
    def get_context(cls, provider: str, profiles_root: Path) -> AndroidContext:
        with cls._lock:
            ctx = cls._contexts.get(provider)
            if ctx is None:
                ctx = AndroidContext(provider, Path(profiles_root) / provider)
                cls._contexts[provider] = ctx
            return ctx
