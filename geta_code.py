"""
Geta Code — نسخهٔ ساده (یک فایل)
اجرا: در VS Code دکمهٔ ▶ (Run Python File) را بزن.
نیاز: یک بار  pip install playwright   (در همان Python که VS Code انتخاب کرده)
"""
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import json
import urllib.request
from pathlib import Path

# ====================== تنظیمات ======================
ROOT = Path(__file__).resolve().parent
# پروفایل‌ها در یک محل ثابت ویندوز می‌مونن، نه داخل پوشه‌ی برنامه.
# اگه داخل پوشه‌ی برنامه بودن، با هر زیپ/پوشه‌ی جدید لاگین‌ها گم می‌شد.
PROFILES = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "GetaCode" / "browser_profiles"
LEGACY_PROFILES = ROOT / "browser_profiles"   # نسخه‌های قبلی اینجا ذخیره می‌کردن؛ یک‌بار منتقل میشن
PROJECTS = ROOT / "projects"           # پروژه‌ها اینجا ساخته می‌شن
MAX_REPAIR = 3
RUN_TIMEOUT = 60
RESPONSE_TIMEOUT = 900          # سقف کلی یک پاسخ (فقط برای پاسخ‌های خیلی طولانی)
AGENT_START_SECONDS = 180       # اگه ایجنت ظرف ۳ دقیقه شروع نکنه، کنار گذاشته میشه
AGENT_IDLE_SECONDS = 180        # اگه ۳ دقیقه هیچ متنی تولید نکنه و در حال تولید هم نباشه، کنار گذاشته میشه
RETRIES = 2                 # تعداد تلاش با هر ایجنت قبل از رفتن سراغ بعدی
RETRY_WAIT = 15             # ثانیه؛ هر تلاش بیشتر صبر می‌کند
BLOCK_SECONDS = 300         # ایجنتی که جواب نداد، این مدت کنار گذاشته می‌شود
RATE_LIMIT_SECONDS = 1800   # ایجنتی که به سقف رسید، این مدت کنار گذاشته می‌شود
DEBUG = ROOT / "DEV"
LIMIT_MARKERS = ["you've reached", "usage limit", "reached the limit", "too many requests"]


from errors import RateLimitError, NeedsLogin, ConversationFull, AgentTimeout  # noqa: E402

# متن‌هایی که وقتی گفتگو پر شده، سرویس‌ها نشون می‌دن (فارسی و انگلیسی)
FULL_MARKERS = ("conversation is too long", "start a new chat", "chat is too long", "context length",
                "maximum context", "context window", "this conversation has reached", "reached the maximum length",
                "گفتگو خیلی طولانی", "گفتگوی جدید را شروع", "گفتگو طولانی", "گفتگوی جدید شروع")


def looks_full(text):
    """اگه پاسخ کوتاه و پیام «گفتگو پر شده» باشه True می‌ده؛ پاسخ واقعی طولانی را رد نمی‌کند."""
    if not text or len(text) > 800:
        return False
    if "@@@" in text or "```" in text:  # پیام پر شدن گفتگو کد ندارد
        return False
    low = text.lower()
    return any(m in low for m in FULL_MARKERS)
import devlog  # noqa: E402
import cancel  # noqa: E402

PROVIDERS = {
    "deepseek": {
        "url": "https://chat.deepseek.com/", "port": 9401,
        "input": "textarea#chat-input, textarea",
        "answer": ".ds-markdown",
        "login": "button:has-text('Log in'), button:has-text('登录')",
        "stop": "button:has-text('Stop'), button:has-text('停止')",
    },
    "chatgpt": {
        "url": "https://chatgpt.com/", "port": 9402,
        "input": "div.ProseMirror[contenteditable='true'][aria-label='Ask ChatGPT'], #prompt-textarea, div.ProseMirror[contenteditable='true'], form [contenteditable='true']",
        "answer": "[data-chatgpt-search-unit-key$=':assistant'], [data-dil-message-id], div[data-message-author-role='assistant']",
        "stop": "button[aria-label='Stop'], button[data-testid='stop-button']",
        "login": "button[data-testid='login-button'], a[href*='/auth/login']",
    },
    "gemini": {
        "url": "https://gemini.google.com/app", "port": 9403,
        "input": "rich-textarea .ql-editor, rich-textarea div[contenteditable='true']",
        "answer": "message-content, .model-response-text",
        "login": "a[href*='accounts.google.com']",
        "stop": "button[aria-label*='Stop' i]",
    },    "claude": {
        "url": "https://claude.ai/new", "port": 9404,
        "input": "div.ProseMirror[contenteditable='true'], div[contenteditable='true'][aria-label], div[contenteditable='true']",
        "answer": "[data-is-streaming], .font-claude-response, [data-testid='assistant-message']",
        "login": "a[href*='/login'], button:has-text('Log in')",
        "stop": "button[aria-label*='Stop' i]",
    },
    "qwen": {
        "url": "https://chat.qwen.ai/", "port": 9405,
        "input": "textarea#chat-input, textarea",
        "answer": ".response-message-content, .markdown-prose",
        "login": "button:has-text('Log in'), button:has-text('登录')",
        "stop": "button:has-text('Stop'), button:has-text('停止')",
    },
    "glm": {
        "url": "https://chat.z.ai/", "port": 9406,
        "start": 300, "idle": 300,  # GLM زمان فکر کردن طولانی داره؛ ۵ دقیقه مهلت
        "input": "textarea, div[contenteditable='true']",
        "answer": ".markdown-prose, .chat-assistant",
        "login": "a[href*='login'], button:has-text('Log in')",
        "stop": "button:has-text('Stop'), button[aria-label*='Stop' i]",
    },
    "kimi": {
        "url": "https://www.kimi.com/", "port": 9407,
        "input": "div[contenteditable='true'], textarea",
        "answer": ".markdown-container, .segment-content",
        "login": "button:has-text('Log in'), a[href*='login']",
        "stop": "button:has-text('Stop'), button[aria-label*='Stop' i]",
    },
}

DEFAULT_ROLES = {"architect": "deepseek", "coder": "deepseek", "reviewer": "chatgpt"}

# ترتیب ترجیحی هر نقش وقتی کاربر «خودکار» انتخاب کرده؛ فقط سرویس‌های وصل‌شده استفاده میشن
AUTO_RANK = {
    "architect": ["claude", "chatgpt", "gemini", "qwen", "glm", "kimi", "deepseek"],
    "coder": ["deepseek", "qwen", "claude", "glm", "chatgpt", "kimi", "gemini"],
    "reviewer": ["chatgpt", "claude", "gemini", "glm", "qwen", "kimi", "deepseek"],
}


# ترجیح سرویس بر اساس نوع فایل: هر بخش از بازی پیش سرویسی می‌ره که اون نوع کار رو بهتر انجام میده
KIND_RANK = {
    "ui": ["qwen", "glm", "claude", "chatgpt", "gemini", "kimi", "deepseek"],     # html / css / رابط
    "logic": ["deepseek", "claude", "qwen", "glm", "chatgpt", "kimi", "gemini"],  # منطق بازی و پایتون
    "data": ["claude", "chatgpt", "gemini", "glm", "qwen", "kimi", "deepseek"],   # تنظیمات و داده
}


def file_kind(path):
    low = path.lower()
    if low.endswith((".html", ".htm", ".css")):
        return "ui"
    if low.endswith((".json", ".yaml", ".yml", ".toml", ".txt", ".md", ".csv")) or "config" in low.rsplit("/", 1)[-1]:
        return "data"
    return "logic"


def prefer_for_file(path, available, last=None):
    """سرویسی که برای این فایل بهتره و وصله. اگه فایل قبلی با همون سرویس نوشته شده، سرویس دیگه‌ای می‌ده."""
    ranked = [p for p in KIND_RANK[file_kind(path)] if p in available and p not in API_PROVIDERS]
    if last and len(ranked) > 1 and ranked[0] == last:
        ranked = ranked[1:] + [ranked[0]]
    return ranked[0] if ranked else None


def auto_roles(available, roles, explicit):
    """نقش‌هایی که کاربر دستی تعیین نکرده رو بین سرویس‌های وصل‌شده پخش می‌کنه.
    تا جایی که ممکنه هر نقش یه سرویس متفاوت می‌گیره تا ایجنت‌ها واقعاً کنار هم کار کنن."""
    out = dict(explicit)
    used = set(explicit.values())
    chat = [p for p in available if p not in API_PROVIDERS]
    for role in ("architect", "coder", "reviewer"):
        if role not in roles or role in out:
            continue
        ranked = [p for p in AUTO_RANK[role] if p in chat] or chat
        fresh = [p for p in ranked if p not in used]
        pick = (fresh or ranked or [None])[0]
        if pick:
            out[role] = pick
            used.add(pick)
    return out

# ====================== مرورگر ایزوله (اندروید + دسکتاپ) ======================
import android_bridge as ab


def _is_android() -> bool:
    return bool(ab.IS_ANDROID or "ANDROID_ARGUMENT" in os.environ or "ANDROID_ROOT" in os.environ)


def _android_storage_root() -> Path:
    try:
        from jnius import autoclass
        activity = autoclass("org.kivy.android.PythonActivity").mActivity
        if activity is not None:
            return Path(str(activity.getFilesDir().getAbsolutePath())) / "GetaCode"
    except Exception:
        pass
    ext = os.environ.get("ANDROID_PRIVATE")
    if ext:
        return Path(ext) / "GetaCode"
    return Path.home() / "GetaCode"


if _is_android():
    _BASE_DATA = _android_storage_root()
    PROFILES = _BASE_DATA / "browser_profiles"
    PROJECTS = Path("/storage/emulated/0/Download/GetaCode_Projects") if Path("/storage/emulated/0/Download").exists() else (_BASE_DATA / "projects")
    DEBUG = _BASE_DATA / "DEV"


def find_browser() -> Path:
    bases = [os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"), os.environ.get("LOCALAPPDATA")]
    for base in filter(None, bases):
        for rel in (r"Google\Chrome\Application\chrome.exe", r"Microsoft\Edge\Application\msedge.exe"):
            path = Path(base) / rel
            if path.exists():
                return path
    raise FileNotFoundError("Chrome یا Edge پیدا نشد.")


def port_alive(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1) as r:
            return r.status == 200
    except Exception:
        return False


def migrate_legacy_profiles():
    """پروفایل‌های قدیمی داخل پوشه‌ی برنامه را یک‌بار به محل ثابت کپی می‌کنه تا لاگین‌ها حفظ بشن."""
    try:
        if not LEGACY_PROFILES.is_dir():
            return
        PROFILES.mkdir(parents=True, exist_ok=True)
        for src in LEGACY_PROFILES.iterdir():
            dst = PROFILES / src.name
            if not src.is_dir() or dst.exists():
                continue
            tmp = PROFILES / (src.name + ".tmp")
            shutil.rmtree(tmp, ignore_errors=True)
            shutil.copytree(src, tmp, ignore=shutil.ignore_patterns("Singleton*", "lockfile", "*.lock"))
            os.replace(tmp, dst)
    except OSError:
        pass


def launch_browser(provider: str) -> int:
    """در دسکتاپ Chrome جدا با پروفایل اختصاصی باز می‌کند و در اندروید WebView ایزوله می‌سازد."""
    port = PROVIDERS[provider]["port"]
    if _is_android():
        (PROFILES / provider).mkdir(parents=True, exist_ok=True)
        return port
    if port_alive(port):
        return port
    migrate_legacy_profiles()
    profile = PROFILES / provider
    profile.mkdir(parents=True, exist_ok=True)
    flags = (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP) if os.name == "nt" else 0
    subprocess.Popen(
        [str(find_browser()), f"--remote-debugging-port={port}", f"--user-data-dir={profile}",
         "--no-first-run", "--no-default-browser-check", PROVIDERS[provider]["url"]],
        creationflags=flags,
    )
    for _ in range(60):
        if port_alive(port):
            return port
        time.sleep(0.5)
    raise RuntimeError(f"Chrome برای {provider} بالا نیامد. اگر پنجرهٔ قبلی Geta برای همین پروفایل بازه، ببندش.")


class Chat:
    """یک محیط وب ایزوله و اختصاصی برای هر سرویس (بدون اتصال به Chrome اصلی گوشی)."""

    def __init__(self, provider: str, pw=None):
        self.name = provider
        self.cfg = PROVIDERS[provider]
        self._pw = pw
        if _is_android():
            self.browser = None
            self.context = ab.AndroidBrowserManager.get_context(provider, PROFILES)
        else:
            port = launch_browser(provider)
            self.browser = self._pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            self.context = self.browser.contexts[0]
        self.page = None
        self.log = print  # Builder بعداً لاگ خودش را می‌دهد
        self.started = False  # بعد از اولین پاسخ، همین صفحه/گفتگو برای همه‌ی درخواست‌های این پروژه استفاده می‌شه
        self.conv_url = None  # گفتگوی ثابت این پروژه؛ فقط یک بار ساخته می‌شود

    def _open_conv(self):
        if self.started and self.page is not None and not self.page.is_closed():
            return self.page
        """اگر گفتگو داریم همان را باز می‌کند؛ وگرنه یک‌بار صفحه‌ی اصلی."""
        if self.page is None or self.page.is_closed():
            self.page = self.context.new_page()
        self.page.goto(self.conv_url or self.cfg["url"], wait_until="domcontentloaded")
        self.page.wait_for_timeout(1500)
        return self.page

    def _remember_conv(self, page):
        """آدرس گفتگو را نگه می‌دارد تا درخواست‌های بعدی در همان چت بروند."""
        try:
            url = page.url or ""
            base = self.cfg["url"].rstrip("/")
            if self.name == "chatgpt":
                if "/c/" in url:
                    self.conv_url = url
            elif url and url.rstrip("/") != base and "login" not in url.lower() and "auth" not in url.lower():
                self.conv_url = url
        except Exception:
            pass

    def _open(self):
        if self.page is None or self.page.is_closed():
            self.page = self.context.new_page()
        self.page.goto(self.cfg["url"], wait_until="domcontentloaded")
        self.page.wait_for_timeout(1500)
        return self.page

    def _input(self, page, timeout=30):
        deadline = time.time() + timeout
        while time.time() < deadline:
            items = page.locator(self.cfg["input"])
            for i in range(items.count()):
                el = items.nth(i)
                try:
                    if el.is_visible() and el.is_enabled():
                        return el
                except Exception:
                    pass
            page.wait_for_timeout(500)
        where = self._debug("no_input")
        raise NeedsLogin(f"ورودی چت {self.name} پیدا نشد. وارد حساب شدی؟ (عکس صفحه: {where}.png)")

    def _mark_full(self):
        """گفتگوی فعلی پر شده؛ دفعه‌ی بعد گفتگوی تازه باز می‌شه و آدرس قبلی دیگه استفاده نمی‌شه."""
        self.started = False
        self.conv_url = None

    def _ask_chatgpt(self, prompt: str) -> str:
        """ChatGPT از ماژول providers/chatgpt.py استفاده می‌کند (خطاها به خطاهای Builder نگاشت می‌شوند)."""
        from providers import chatgpt as cg
        page = self._open_conv()
        fresh = self.conv_url is None and not self.started
        try:
            text = cg.ask(page, prompt, log=self.log, timeout=RESPONSE_TIMEOUT, new_chat=fresh)
        except cg.ChatGPTLimit as e:
            self._remember_conv(page)
            raise RateLimitError(str(e)) from e
        except cg.ChatGPTLoginRequired as e:
            raise NeedsLogin(str(e)) from e
        except cg.ChatGPTError as e:
            self._remember_conv(page)
            raise RuntimeError(str(e)) from e
        if looks_full(text):
            self._mark_full()
            raise ConversationFull(f"گفتگوی {self.name} پر شده است")
        self._remember_conv(page)
        self.started = True
        return text

    def _has_input(self, page):
        items = page.locator(self.cfg["input"])
        for i in range(min(items.count(), 5)):
            try:
                if items.nth(i).is_visible():
                    return True
            except Exception:
                pass
        return False

    def _login_visible(self, page):
        sel = self.cfg.get("login")
        if not sel:
            return False
        try:
            loc = page.locator(sel)
            return any(loc.nth(i).is_visible() for i in range(min(loc.count(), 3)))
        except Exception:
            return False

    def _debug(self, tag):
        try:
            DEBUG.mkdir(exist_ok=True)
            base = DEBUG / f"{self.name}_{tag}_{time.strftime('%H%M%S')}"
            self.page.screenshot(path=str(base) + ".png", full_page=True)
            base.with_suffix(".html").write_text(self.page.content(), encoding="utf-8")
            return str(base)
        except Exception:
            return "(ذخیره نشد)"

    def wait_for_login(self, timeout=300):
        """صفحه ایزوله را باز می‌کند و تا وقتی ورود انجام نشده صبر می‌کند؛ سپس WebView را مخفی می‌کند."""
        page = self._open()
        if hasattr(page, "set_overlay_mode"):
            page.set_overlay_mode(True)
        try:
            deadline = time.time() + timeout
            while time.time() < deadline:
                if cancel.STOP.is_set():
                    return False
                if self._has_input(page) and not self._login_visible(page):
                    return True
                page.wait_for_timeout(2000)
            return False
        finally:
            if hasattr(page, "set_overlay_mode"):
                page.set_overlay_mode(False)

    def _count(self, page):
        try:
            return page.locator(self.cfg["answer"]).count()
        except Exception:
            return 0

    def _last(self, page):
        """تبدیل پاسخ آخر DeepSeek/Gemini به Markdown با حفظ کامل بلاک‌های کد و تورفتگی‌های پایتون."""
        try:
            ans_sel = self.cfg["answer"]
            md = page.evaluate("""(sel) => {
                const all = [...document.querySelectorAll(sel)];
                if (!all.length) return '';
                const root = all[all.length - 1];

                const codeOf = (pre) => {
                    const code = pre.querySelector('code');
                    let lang = '';
                    if (code) {
                        const m = (code.className || '').match(/language-([\\w+#.\\-]+)/);
                        if (m) lang = m[1];
                    }
                    const cm = pre.querySelector('.cm-content');
                    let text = '';
                    if (cm) text = [...cm.querySelectorAll('.cm-line')].map(l => l.textContent).join('\\n');
                    else if (code) text = code.textContent || '';
                    else {
                        const clone = pre.cloneNode(true);
                        clone.querySelectorAll('button, svg, .ds-markdown-code-copy-button, .md-code-block-banner').forEach(e => e.remove());
                        text = clone.textContent || clone.innerText || '';
                    }
                    return {lang, text: text.replace(/\\n+$/, '')};
                };

                const walk = (node) => {
                    if (node.nodeType === 3) return node.nodeValue;
                    if (node.nodeType !== 1) return '';
                    const tag = node.tagName.toLowerCase();
                    if (['button', 'svg', 'style', 'script'].includes(tag)) return '';
                    if (node.classList && (node.classList.contains('md-code-block-banner') || node.classList.contains('ds-markdown-code-copy-button'))) return '';
                    if (tag === 'pre') {
                        const c = codeOf(node);
                        const fence = c.text.includes('```') ? '````' : '```';
                        return '\\n\\n' + fence + c.lang + '\\n' + c.text + '\\n' + fence + '\\n\\n';
                    }
                    const inner = () => [...node.childNodes].map(walk).join('');
                    if (/^h[1-6]$/.test(tag)) return '\\n\\n' + '#'.repeat(+tag[1]) + ' ' + inner().trim() + '\\n\\n';
                    switch (tag) {
                        case 'br': return '\\n';
                        case 'p': return '\\n\\n' + inner() + '\\n\\n';
                        case 'hr': return '\\n\\n---\\n\\n';
                        case 'code': return '`' + (node.textContent || '') + '`';
                        case 'li': return '\\n- ' + inner().trim();
                        default: return inner();
                    }
                };

                const hasPre = !!root.querySelector('pre');
                if (hasPre) {
                    const res = walk(root).replace(/\\n{3,}/g, '\\n\\n').trim();
                    if (res) return res;
                }
                const clone = root.cloneNode(true);
                clone.querySelectorAll('button, svg').forEach(e => e.remove());
                return (clone.innerText || clone.textContent || '').trim();
            }""", ans_sel)
            return (md or "").strip()
        except Exception:
            try:
                loc = page.locator(self.cfg["answer"])
                n = loc.count()
                return loc.nth(n - 1).inner_text().strip() if n else ""
            except Exception:
                return ""

    def _generating(self, page):
        stop = page.locator(self.cfg["stop"])
        for i in range(stop.count()):
            try:
                if stop.nth(i).is_visible():
                    return True
            except Exception:
                pass
        return False

    def _click_continue_if_present(self, page) -> bool:
        """تشخیص و کلیک خودکار روی دکمه‌ی Continue / ادامه در DeepSeek وقتی پیام طولانی متوقف می‌شود."""
        try:
            clicked = page.evaluate("""() => {
                const vis = (el) => {
                    if (!el) return false;
                    const r = el.getBoundingClientRect();
                    const s = getComputedStyle(el);
                    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
                };
                const labels = ['continue', 'continue generating', 'keep generating', 'پاسخ را ادامه بده', 'ادامه', '继续', '继续生成'];
                const candidates = [
                    ...document.querySelectorAll('button, [role="button"], .ds-button, .ds-icon-button, div, span, a')
                ];
                for (const el of candidates) {
                    if (!vis(el)) continue;
                    if (el.closest('nav, aside, header')) continue;
                    if (el.children.length > 2) continue;
                    // پیام‌های کاربر (از جمله خود دستور Continue) هرگز دکمه نیستند
                    if (el.closest('[data-message-author-role=\"user\"], [data-role=\"user\"], [class*=\"user\"], [class*=\"User\"]')) continue;
                    const txt = ((el.innerText || el.textContent || '') + ' ' + (el.getAttribute('aria-label') || '')).trim().toLowerCase();
                    if (txt.length > 40) continue;
                    if (labels.some(l => txt === l || txt === l + '…' || txt === l + '...' || txt === '继续' || txt === '继续生成' || txt.startsWith('continue generating'))) {
                        el.scrollIntoView({block: 'center'});
                        el.click();
                        return txt || 'continue';
                    }
                }
                return '';
            }""")
            now = time.time()
            if clicked and now - getattr(self, "_last_continue_click", 0) < 10:
                return False  # جلوی کلیک‌های پشت‌سرهم روی همون دکمه رو می‌گیریم
            if clicked:
                self._last_continue_click = now
                self.log(f"[{self.name}] ↻ دکمه‌ی Continue تشخیص داده شد و خودکار کلیک شد ({clicked})…")
                devlog.event(f"{self.name.upper()}_CONTINUE_CLICKED", f"کلیک خودکار روی دکمه‌ی Continue ({clicked})")
                return True
        except Exception:
            pass
        return False

    def _send_box_message(self, page, box, prompt: str):
        """ارسال مطمئن پیام در DeepSeek/Gemini حتی برای پرامپت‌های طولانی ۲۰ هزار کاراکتری."""
        box.click()
        filled = 0
        try:
            # در DeepSeek استفاده از value setter مستقیم روی textarea مانع از تبدیل متن طولانی به فایل پیوست (Paste as file) می‌شود
            filled = page.evaluate("""(t) => {
                const el = document.querySelector('textarea#chat-input, textarea') || document.activeElement;
                if (!el) return 0;
                el.focus();
                if (el.tagName === 'TEXTAREA') {
                    const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set;
                    setter.call(el, t);
                    el.dispatchEvent(new InputEvent('input', {bubbles: true, inputType: 'insertText', data: t}));
                    el.dispatchEvent(new Event('change', {bubbles: true}));
                    return el.value.length;
                }
                const sel = window.getSelection();
                const range = document.createRange();
                range.selectNodeContents(el);
                sel.removeAllRanges();
                sel.addRange(range);
                document.execCommand('delete', false, null);
                const dt = new DataTransfer();
                dt.setData('text/plain', t);
                el.dispatchEvent(new ClipboardEvent('paste', {clipboardData: dt, bubbles: true, cancelable: true}));
                return (el.innerText || '').length;
            }""", prompt) or 0
        except Exception:
            filled = 0

        if filled < len(prompt.strip()) * 0.8:
            try:
                box.fill(prompt)
            except Exception:
                page.keyboard.insert_text(prompt)

        page.wait_for_timeout(450)
        page.keyboard.press("Enter")
        page.wait_for_timeout(600)

        # بررسی اینکه آیا متن هنوز داخل کادر مانده است (اگر Enter ارسال نکرده بود، روی دکمه‌ی Send کلیک کن)
        try:
            rem = page.evaluate("""() => {
                const el = document.querySelector('textarea#chat-input, textarea');
                return el ? (el.value || '').trim().length : 0;
            }""") or 0
            if rem > 20:
                page.evaluate("""() => {
                    const el = document.querySelector('textarea#chat-input, textarea');
                    const container = el ? (el.closest('form') || el.parentElement.parentElement.parentElement) : document.body;
                    const btns = [...(container || document.body).querySelectorAll('div[role="button"], button, .ds-icon-button')];
                    for (let i = btns.length - 1; i >= 0; i--) {
                        const b = btns[i];
                        if (b.getAttribute('aria-disabled') === 'true' || b.disabled) continue;
                        const r = b.getBoundingClientRect();
                        if (r.width > 16 && r.height > 16 && b.querySelector('svg')) {
                            b.click();
                            return true;
                        }
                    }
                    return false;
                }""")
                page.wait_for_timeout(400)
                page.keyboard.press("Enter")
        except Exception:
            pass

    def ask(self, prompt: str) -> str:
        if self.name == "chatgpt":
            return self._ask_chatgpt(prompt)
        page = self._open_conv()
        try:
            page.bring_to_front()
        except Exception:
            pass

        # اگر از نوبت قبلی دکمه‌ی Continue یا توقف مانده، اول وضعیت را تمیز کن
        box = self._input(page)
        before = self._count(page)
        before_last_text = self._last(page) if before > 0 else ""

        self._send_box_message(page, box, prompt)
        self.log(f"[{self.name}] پرامپت ارسال شد ({len(prompt)} کاراکتر)")
        devlog.event(f"{self.name.upper()}_SENT", f"پرامپت به {self.name} ارسال شد ({len(prompt)} کاراکتر)")

        start_wait = time.time()
        deadline = start_wait + self.cfg.get("start", AGENT_START_SECONDS)
        last_report = start_wait
        retried_send = False

        while self._count(page) <= before:
            cancel.check()
            now = time.time()
            # اگر در همان باکس قبلی متن تغییر کرده یا دکمه تولید روشن شده، یعنی پاسخ شروع شده است
            cur_last = self._last(page)
            if (cur_last and cur_last != before_last_text) or self._generating(page):
                break
            # اگر بعد از ۱۰ ثانیه هنوز هیچ پاسخی شروع نشده، یک بار دیگر ارسال/کلیک دکمه Send را امتحان کن
            if not retried_send and (now - start_wait) >= 10:
                retried_send = True
                try:
                    rem = page.evaluate("""() => {
                        const el = document.querySelector('textarea#chat-input, textarea');
                        return el ? (el.value || '').trim().length : 0;
                    }""") or 0
                    if rem > 10:
                        self.log(f"[{self.name}] تلاش مجدد برای فشردن دکمه‌ی ارسال…")
                        self._send_box_message(page, box, prompt)
                    else:
                        page.keyboard.press("Enter")
                except Exception:
                    pass
            if now > deadline:
                where = self._debug("no_reply")
                raise AgentTimeout(f"{self.name} شروع به پاسخ نکرد (عکس: {where}.png)")
            if now - last_report >= 15:
                self.log(f"[{self.name}] در انتظار شروع پاسخ… ({int(now - start_wait)}s)")
                last_report = now
            page.wait_for_timeout(500)

        last, stable = "", 0
        last_growth = time.time()
        continue_clicks = 0
        MAX_CONTINUES = 6
        accumulated_parts = []

        while time.time() < deadline:
            cancel.check()
            page.wait_for_timeout(1000)
            now = time.time()
            text = self._last(page)
            gen = self._generating(page)
            if text != last:
                last_growth = now
            idle_limit = self.cfg.get("idle", AGENT_IDLE_SECONDS)
            if not gen and now - last_growth > idle_limit:
                where = self._debug("idle")
                raise AgentTimeout(f"{self.name} بیش از {int(idle_limit) // 60} دقیقه پیشرفتی نداشت (عکس: {where}.png)")
            if now - last_report >= 15:
                self.log(f"[{self.name}] در حال دریافت پاسخ… ({int(now - start_wait)}s | طول: {len(text)} کاراکتر)")
                last_report = now

            if text and text == last and not gen:
                stable += 1
                # ۱. اول بررسی کن آیا دکمه‌ی Continue در صفحه ظاهر شده است؟
                if stable >= 2 and continue_clicks < MAX_CONTINUES:
                    if self._click_continue_if_present(page):
                        continue_clicks += 1
                        stable = 0
                        deadline = max(deadline, time.time() + 300)
                        page.wait_for_timeout(1500)
                        continue

                # ۲. اگر کد با @@@FILE شروع شده ولی هنوز @@@END نیامده و دکمه‌ی Continue هم نبود،
                # بعد از ۴ ثانیه توقف، خودکار دستور "Continue" بفرست تا ادامه‌ی کد را بنویسد!
                needs_end = ("@@@FILE" in text and not text.rstrip().endswith("@@@END"))
                if needs_end and stable >= 4 and continue_clicks < MAX_CONTINUES:
                    continue_clicks += 1
                    self.log(f"[{self.name}] ↻ پاسخ طولانی نصفه ماند؛ ارسال خودکار دستور ادامه ({continue_clicks}/{MAX_CONTINUES})…")
                    devlog.event(f"{self.name.upper()}_AUTO_CONTINUE_PROMPT", f"ارسال پیام ادامه برای تکمیل فایل (طول فعلی: {len(text)})")
                    accumulated_parts.append(text)
                    try:
                        prev_cnt = self._count(page)
                        c_box = self._input(page, timeout=10)
                        self._send_box_message(
                            page, c_box,
                            "Continue writing the exact remaining code from the exact line where it cut off, inside a ``` code block, and finish with @@@END. Do not repeat the beginning."
                        )
                        # صبر کوتاه برای شروع پیام بعدی
                        w_end = time.time() + 25
                        while time.time() < w_end and self._count(page) <= prev_cnt and not self._generating(page):
                            page.wait_for_timeout(500)
                        stable = 0
                        last = ""
                        deadline = max(deadline, time.time() + 300)
                        continue
                    except Exception as e:
                        devlog.error(f"{self.name}.auto_continue", e)

                req_stable = 5 if needs_end else 3
                if stable >= req_stable:
                    low = text.lower()
                    if len(text) < 400 and any(m in low for m in LIMIT_MARKERS):
                        raise RateLimitError(f"{self.name} به سقف استفاده رسیده است")
                    if looks_full(text):
                        self._mark_full()
                        raise ConversationFull(f"گفتگوی {self.name} پر شده است")
                    self._remember_conv(page)
                    self.started = True
                    final_text = "\n".join(accumulated_parts + [text]) if accumulated_parts else text
                    self.log(f"[{self.name}] پاسخ کامل شد ({int(now - start_wait)}s، طول: {len(final_text)} کاراکتر)")
                    return final_text
            else:
                stable = 0
            last = text
        where = self._debug("timeout")
        raise AgentTimeout(f"{self.name} پاسخ را کامل نکرد (عکس: {where}.png)")

    def close(self):
        self.page = None

# ====================== پرامپت‌ها ======================
FILE_FORMAT = """قالب خروجی فایل (دقیقاً همین):
<<<FILE path="مسیر/نسبی/فایل.py">>>
محتوای کامل فایل
<<<END>>>
هیچ متن اضافه یا ``` بیرون از بلوک‌ها ننویس. کد باید کامل باشد."""

ARCHITECT = """تو معمار نرم‌افزار هستی. فقط یک JSON معتبر برگردان، بدون توضیح و بدون ```.
{"name": "snake_case", "entry": "main.py", "files": [{"path": "main.py", "purpose": "..."}],
 "requirements": [], "run": "python main.py", "exe": false, "exe_name": "AppName"}
قوانین: حداکثر ۱۰ فایل؛ requirements.txt را در files نیاور؛ exe را فقط وقتی true کن که کاربر خواسته.
درخواست کاربر:
"""

CODER = "تو برنامه‌نویس پایتون هستی. فقط فایل زیر را کامل بنویس.\n" + FILE_FORMAT + """

درخواست کلی: {task}
طرح پروژه: {plan}
فایل فعلی: {path}
هدف: {purpose}
فایل‌های قبلی:
{existing}
"""

REVIEWER = """تو بازبین کد هستی. باگ، importهای ناقص و ناسازگاری بین فایل‌ها را بررسی کن.
اگر مشکلی نیست فقط بنویس: OK
اگر هست، فقط فایل‌های اصلاح‌شده را کامل برگردان.
""" + FILE_FORMAT + """

درخواست: {task}
پروژه:
{files}
"""

FIXER = "برنامه با خطا اجرا شد. فایل(های) لازم را اصلاح کن.\n" + FILE_FORMAT + """

خطا:
{error}
فایل‌های فعلی:
{files}
"""

# ====================== منطق ساخت ======================
FILE_RE = re.compile(r'<<<FILE path="([^"]+)">>>\s*\n(.*?)\n?<<<END>>>', re.S)


def parse_files(text):
    return [(m.group(1).strip(), m.group(2).rstrip() + "\n") for m in FILE_RE.finditer(text)]


def parse_json(text):
    s, e = text.find("{"), text.rfind("}")
    if s < 0 or e <= s:
        raise ValueError("JSON پیدا نشد")
    return json.loads(text[s:e + 1])


def safe_path(root: Path, rel: str) -> Path:
    p = Path(rel)
    if not rel.strip() or p.is_absolute() or ".." in p.parts:
        raise ValueError(f"مسیر نامعتبر: {rel}")
    dest = (root / p).resolve()
    if root.resolve() not in dest.parents:
        raise ValueError(f"مسیر خارج از پروژه: {rel}")
    return dest


def run_project(root: Path, command: str):
    if command.startswith("python "):
        command = f'"{sys.executable}" ' + command[len("python "):]
    try:
        r = subprocess.run(command, shell=True, cwd=root, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=RUN_TIMEOUT)
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired as e:
        out = e.stdout.decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        return 0, out, "(زمان اجرا تمام شد؛ احتمالاً برنامه‌ی گرافیکی بالا آمده)"


# ====================== API (OpenRouter و ...) ======================
# کلید API در فایل تنظیمات کنار پروفایل‌ها ذخیره می‌شه، نه داخل کد.
CONFIG_FILE = (_android_storage_root() if _is_android() else (Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "GetaCode")) / "config.json"
CONFIG_DEFAULT = {
    "openrouter_api_key": "",
    "openrouter_model": "openrouter/auto",
    "gemini_api_key": "",
    "gemini_model": "gemini-2.5-flash",
    "use_api_first": True,
    "chat_char_budget": 100000,
    "_help": "کلید OpenRouter را داخل openrouter_api_key و کلید Gemini را داخل gemini_api_key بگذار. هرکدام خالی باشه، استفاده نمی‌شه."
}


def load_config():
    try:
        if not CONFIG_FILE.exists():
            CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
            CONFIG_FILE.write_text(json.dumps(CONFIG_DEFAULT, ensure_ascii=False, indent=2), encoding="utf-8")
        cfg = dict(CONFIG_DEFAULT)
        cfg.update(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
        return cfg
    except (OSError, ValueError):
        return dict(CONFIG_DEFAULT)


# سرویس‌های API سازگار با OpenAI. هرکدوم کلید داشته باشه، برای هماهنگ‌کننده استفاده میشه.
# برای اضافه کردن سرویس جدید فقط یه مدخل اینجا اضافه کن.
API_PROVIDERS = {
    "openrouter": {"url": "https://openrouter.ai/api/v1/chat/completions", "key": "openrouter_api_key",
                   "model": "openrouter_model", "default_model": "openrouter/auto", "history": "api_history.json"},
    "gemini_api": {"url": "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions", "key": "gemini_api_key",
               "model": "gemini_model", "default_model": "gemini-2.5-flash", "history": "api_history_gemini.json"},
}


def api_key(provider):
    return str(load_config().get(API_PROVIDERS[provider]["key"], "")).strip()


def configured_api_providers():
    return [p for p in API_PROVIDERS if api_key(p)]


def api_configured():
    return bool(configured_api_providers())


class ApiChat:
    """گفتگوی API برای یک پروژه؛ تاریخچه‌ی پیام‌ها داخل .geta/api_history.json می‌مونه تا همه‌ی مراحل یک گفتگو باشن."""
    MAX_MESSAGES = 60

    def __init__(self, history_file, provider="openrouter"):
        spec = API_PROVIDERS[provider]
        cfg = load_config()
        self.provider = provider
        self.URL = spec["url"]
        self.key = api_key(provider)
        self.model = str(cfg.get(spec["model"]) or spec["default_model"])
        self.history_file = Path(history_file)
        self.conv_url = None  # API گفتگوی آدرس‌دار ندارد؛ فقط برای سازگاری با ذخیره‌ی گفتگو
        try:
            self.messages = json.loads(self.history_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.messages = []

    def ask(self, prompt):
        messages = self.messages + [{"role": "user", "content": prompt}]
        body = json.dumps({"model": self.model, "messages": messages}).encode("utf-8")
        req = urllib.request.Request(self.URL, data=body, method="POST", headers={
            "Authorization": f"Bearer {self.key}",
            "Content-Type": "application/json",
            "X-Title": "Geta Code",
        })
        devlog.event("OPENROUTER_REQ", f"ارسال درخواست به OpenRouter (مدل: {self.model}، طول پرامپت: {len(prompt)} کاراکتر)")
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                data = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = json.loads(e.read().decode("utf-8")).get("error", {}).get("message", "")
            except Exception:
                pass
            if e.code == 429 or e.code == 402:
                raise RateLimitError(f"OpenRouter سقف استفاده یا اعتبار را زد ({e.code}). {detail}")
            if e.code in (400, 413) and any(k in detail.lower() for k in ("context", "too long", "maximum", "token")):
                self.messages = []
                try:
                    self.history_file.write_text("[]", encoding="utf-8")
                except OSError:
                    pass
                raise ConversationFull(f"تاریخچه‌ی OpenRouter پر شده بود ({e.code})")
            if e.code in (401, 403):
                raise RuntimeError(f"کلید OpenRouter قبول نشد ({e.code}). کلید را در config.json چک کن.")
            raise RuntimeError(f"OpenRouter خطای {e.code}: {detail}")
        text = data["choices"][0]["message"]["content"] or ""
        devlog.event("OPENROUTER_RES", f"پاسخ OpenRouter دریافت شد ({len(text)} کاراکتر)", text[:2000])
        if not text.strip():
            raise RuntimeError("OpenRouter پاسخ خالی داد.")
        self.messages = (messages + [{"role": "assistant", "content": text}])[-self.MAX_MESSAGES:]
        try:
            self.history_file.parent.mkdir(parents=True, exist_ok=True)
            self.history_file.write_text(json.dumps(self.messages, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass
        return text

    def close(self):
        pass


CHAT_CHAR_BUDGET = int(load_config().get("chat_char_budget", 100_000))  # تقریب حجم هر گفتگو (کاراکتر)


class Builder:
    def __init__(self, roles, root, log, force_exe, allow_run, available=None):
        self.roles, self.root, self.log = dict(roles), root, log
        # هماهنگ‌کننده (تحلیل، پرامپت‌نویسی، یکپارچه‌سازی و اصلاح بیلد) با OpenRouter؛ اگه نبود، سرویس‌های چت
        apis = configured_api_providers()
        self.roles.setdefault("orchestrator", apis[0] if apis else "openrouter")
        # شمارش تقریبی حجم گفتگو هر سرویس؛ وقتی از بودجه رد شد، گفتگوی تازه با خلاصه شروع می‌شه
        self.chat_chars = {k[len("_chars_"):]: int(v) for k, v in self._saved_convs().items() if k.startswith("_chars_")}
        self.available = list(PROVIDERS) if available is None else list(available)
        for p in reversed(apis):
            if p not in self.available:
                self.available.insert(0, p)
        self._blocked = {}
        self.force_exe, self.allow_run = force_exe, allow_run
        self.files = {}
        self.chats = {}
        self._pw = None
        self.last_provider = None

    def playwright(self):
        if _is_android():
            return None
        # فقط یک بار در کل این نخ ساخته میشه (ساختن دوباره باعث خطای asyncio می‌شد)
        if self._pw is None:
            from playwright.sync_api import sync_playwright
            self._pw = sync_playwright().start()
        return self._pw

    def _conv_file(self):
        return Path(self.root) / ".geta" / "conv.json"

    def _saved_convs(self):
        try:
            return json.loads(self._conv_file().read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _forget_conv(self, provider):
        chat = self.chats.get(provider)
        if chat is not None:
            chat.started = False
            chat.conv_url = None
        self.chat_chars[provider] = 0
        try:
            data = self._saved_convs()
            data.pop(provider, None)
            data.pop("_chars_" + provider, None)
            self._conv_file().parent.mkdir(parents=True, exist_ok=True)
            self._conv_file().write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass

    def _save_conv(self, provider):
        chat = self.chats.get(provider)
        try:
            data = self._saved_convs()
            if chat is not None and chat.conv_url:
                data[provider] = chat.conv_url
            data["_chars_" + provider] = self.chat_chars.get(provider, 0)
            self._conv_file().parent.mkdir(parents=True, exist_ok=True)
            self._conv_file().write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass

    def _api_chat(self, provider):
        if provider not in self.chats:
            self.log(f"[API] گفتگوی {provider} برای این پروژه باز شد.")
            self.chats[provider] = ApiChat(Path(self.root) / ".geta" / API_PROVIDERS[provider]["history"], provider)
        return self.chats[provider]

    def _chat(self, provider):
        if provider in API_PROVIDERS:
            return self._api_chat(provider)
        if provider not in self.chats:
            self.log(f"اتصال به {provider} ...")
            self.chats[provider] = Chat(provider, self.playwright())
            self.chats[provider].log = self.log
            self.chats[provider].conv_url = self._saved_convs().get(provider)
        return self.chats[provider]

    def ask(self, role, prompt, prefer=None):
        preferred = self.roles[role]
        if prefer and role in getattr(self, "auto_roles", set()):
            preferred = prefer
        order = [preferred] + [p for p in self.available if p != preferred]
        order = [p for p in order if p in self.available]
        api_names = set(API_PROVIDERS)
        if role == "orchestrator":
            order = [p for p in order if p in api_names] + [p for p in order if p not in api_names]
        else:
            # سرویس‌های API فقط برای هماهنگ‌کننده‌اند؛ کدنویسی همچنان با سرویس‌های چت انجام میشه
            order = [p for p in order if p not in api_names]
        candidates = [p for p in order if self._blocked.get(p, 0) < time.time()] or order
        last_err = None

        devlog.event("BUILDER_ASK", f"درخواست برای نقش «{role}» | کاندیداها: {candidates}")
        for provider in candidates:
            for attempt in range(1, RETRIES + 1):
                try:
                    devlog.event("PROVIDER_TRY", f"تلاش {attempt}/{RETRIES} با سرویس {provider} برای نقش {role}")
                    if self.chat_chars.get(provider, 0) > CHAT_CHAR_BUDGET:
                        self.log(f"ℹ حافظه‌ی گفتگوی {provider} پر شد؛ گفتگوی تازه با خلاصه شروع می‌شود.")
                        self._forget_conv(provider)
                        raise ConversationFull(f"بودجه‌ی گفتگوی {provider} تمام شد")
                    text = self._chat(provider).ask(prompt)
                    self.chat_chars[provider] = self.chat_chars.get(provider, 0) + len(prompt) + len(text or "")
                    self.last_provider = provider
                    self._save_conv(provider)
                    if not text.strip():
                        raise RuntimeError("پاسخ خالی برگشت")
                    if provider != preferred:
                        self.log(f"✓ کار {role} با {provider} انجام شد (به جای {preferred}).")
                    return text
                except RateLimitError as e:
                    last_err = e
                    self.log(f"⚠ {provider} به سقف استفاده رسید؛ {RATE_LIMIT_SECONDS // 60} دقیقه کنار گذاشته شد.")
                    self._blocked[provider] = time.time() + RATE_LIMIT_SECONDS
                    break
                except ConversationFull:
                    self._forget_conv(provider)
                    raise
                except AgentTimeout as e:
                    last_err = e
                    self.log(f"⏱ {provider} ظرف مدت مجاز کاری نکرد؛ سراغ ایجنت بعدی می‌رویم.")
                    self._blocked[provider] = time.time() + BLOCK_SECONDS
                    break
                except NeedsLogin as e:
                    last_err = e
                    self.log(f"⚠ {provider} وارد حساب نیست؛ از ایجنت بعدی استفاده می‌شود. (از پنجره‌ی همان سرویس وارد شو)")
                    self._blocked[provider] = time.time() + BLOCK_SECONDS
                    break
                except Exception as e:
                    last_err = e
                    devlog.error(f"Builder.ask ({provider}/{role})", e)
                    self.log(f"⚠ {provider} برای {role} جواب نداد ({attempt}/{RETRIES}): {e}")
                    if attempt < RETRIES:
                        wait = RETRY_WAIT * attempt
                        self.log(f"صبر {wait} ثانیه و تلاش دوباره با {provider} ...")
                        time.sleep(wait)
            else:
                self._blocked[provider] = time.time() + BLOCK_SECONDS
                self.log(f"[{role}] رفتن سراغ ایجنت بعدی ...")

        if isinstance(last_err, NeedsLogin):
            raise NeedsLogin(f"هیچ ایجنت وارد‌شده‌ای در دسترس نیست. {last_err}")
        raise RuntimeError(f"هیچ ایجنتی نتوانست کار «{role}» را انجام دهد. آخرین خطا: {last_err}")

    def ask_files(self, role, prompt):
        for attempt in range(2):
            if attempt:
                prompt = "قالب را دقیقاً رعایت کن و فقط بلوک‌های <<<FILE>>> بده.\n\n" + prompt
            out = parse_files(self.ask(role, prompt))
            if out:
                return out
        return []

    def write(self, rel, body):
        dest = safe_path(self.root, rel)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(body, encoding="utf-8")
        self.files[rel] = body
        self.log(f"✓ نوشته شد: {rel}")

    def snapshot(self):
        return "\n\n".join(f"### FILE: {p}\n{b}" for p, b in self.files.items())[:40000]

    def run(self, task):
        self.log("معمار در حال طراحی ...")
        plan = None
        for _ in range(2):
            try:
                plan = parse_json(self.ask("architect", ARCHITECT + task))
                break
            except ValueError:
                pass
        if plan is None:
            raise RuntimeError("معمار JSON معتبر برنگرداند.")
        if self.force_exe:
            plan["exe"] = True
        plan.setdefault("files", [])
        plan.setdefault("entry", "main.py")
        plan_json = json.dumps(plan, ensure_ascii=False, indent=2)
        self.log(f"طرح: {len(plan['files'])} فایل | exe={plan.get('exe')}")

        if plan.get("requirements"):
            self.write("requirements.txt", "\n".join(plan["requirements"]) + "\n")

        for item in plan["files"]:
            rel = item.get("path", "")
            if not rel or rel == "requirements.txt":
                continue
            self.log(f"کدنویسی {rel} ...")
            outs = self.ask_files("coder", CODER.format(
                task=task, plan=plan_json, path=rel, purpose=item.get("purpose", ""),
                existing=self.snapshot() or "(هنوز نیست)"))
            if not outs:
                raise RuntimeError(f"کدنویس برای {rel} خروجی معتبر نداد.")
            for p, b in outs:
                self.write(p, b)

        self.review(task)

        cmd = plan.get("run") or ""
        if cmd and self.allow_run:
            self.install_requirements()
            self.run_and_repair(task, cmd)
        elif cmd:
            self.log("اجرای خودکار خاموش است؛ دستور اجرا: " + cmd)

        if plan.get("exe"):
            self.build_exe(plan["entry"], plan.get("exe_name") or plan["name"])
        self.log(f"✔ پروژه آماده است: {self.root}")

    def review(self, task):
        for _ in range(MAX_REPAIR):
            self.log("بازبینی ...")
            reply = self.ask("reviewer", REVIEWER.format(task=task, files=self.snapshot()))
            fixes = parse_files(reply)
            if not fixes:
                self.log("بازبین تأیید کرد.")
                return
            for p, b in fixes:
                self.write(p, b)

    def install_requirements(self):
        req = self.root / "requirements.txt"
        if req.exists():
            self.log("نصب requirements.txt ...")
            subprocess.run([sys.executable, "-m", "pip", "install", "-r", str(req)], cwd=self.root, timeout=900)

    def run_and_repair(self, task, command):
        for attempt in range(MAX_REPAIR + 1):
            code, out, err = run_project(self.root, command)
            if code == 0:
                self.log("✔ اجرا موفق بود.")
                if out.strip():
                    self.log(out.strip()[-800:])
                return
            self.log(f"✗ اجرا خطا داد (کد {code}).")
            if attempt == MAX_REPAIR:
                break
            self.log("اصلاح بر اساس خطا ...")
            fixes = self.ask_files("coder", FIXER.format(
                error=((err or "") + "\n" + (out or ""))[-4000:], files=self.snapshot()))
            if not fixes:
                break
            for p, b in fixes:
                self.write(p, b)

    def build_exe(self, entry, name):
        self.log("ساخت exe با PyInstaller (چند دقیقه طول می‌کشه) ...")
        subprocess.run([sys.executable, "-m", "pip", "install", "pyinstaller"], check=True, timeout=900)
        subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--onefile", "--name", name, entry],
                       cwd=self.root, check=True, timeout=1800)
        built = self.root / "dist" / f"{name}.exe"
        if built.exists():
            shutil.copy2(built, self.root / f"{name}.exe")
            self.log(f"✔ فایل exe: {self.root / (name + '.exe')}")
        else:
            self.log("✗ فایل exe ساخته نشد.")

    def close(self):
        for chat in self.chats.values():
            chat.close()
        if self._pw is not None:
            try:
                self._pw.stop()
            except Exception:
                pass
            self._pw = None
