# -*- coding: utf-8 -*-
"""
providers/chatgpt.py — ارتباط فوق‌مقاوم و بدون باگ با ChatGPT (Chrome جدا + CDP)

رفع کامل مشکلات گزارش‌شده:
- تفکیک ۱۰۰٪ پیام کاربر از پیام دستیار (حذف هرگونه تداخل با [data-user-message-bubble] و پیام‌های قبلی)
- جلوگیری از کلیک اشتباهی روی دکمه‌ی Copy پیام کاربر (اول استخراج مستقیم از DOM دستیار، سپس Copy فقط از actionRow دستیار)
- اطمینان از ارسال کامل پرامپت و خالی شدن کادر قبل از شروع انتظار
- ثبت تمام جزئیات فنی و وضعیت صفحه در پوشه‌ی DEV/
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from errors import AgentTimeout

AGENT_IDLE_SECONDS = 180  # ۳ دقیقه بدون پیشرفت یعنی این ایجنت کار نکرده؛ سراغ بعدی
from typing import Callable, Optional

try:
    import cancel as _cancel
except Exception:
    _cancel = None

try:
    import devlog as _devlog
except Exception:
    _devlog = None

BASE_URL = "https://chatgpt.com/"
ROOT_DIR = Path(__file__).resolve().parent.parent
DEBUG_DIR = ROOT_DIR / "DEV"

REPORT_EVERY = 12
DEBUG_AFTER = 55
STABLE_DONE = 3.0
STABLE_EARLY = 1.8
STABLE_BLIND = 7.0
STOP_SUSPECT = 22.0

COMPOSER_SELECTORS = [
    "div.ProseMirror[contenteditable='true'][aria-label='Ask ChatGPT']",
    "div[contenteditable='true'][aria-label='Ask ChatGPT']",
    "#prompt-textarea",
    "div[contenteditable='true']#prompt-textarea",
    "textarea[name='prompt-textarea']",
    "form div[contenteditable='true']",
    "form textarea",
]
SEND_SELECTORS = [
    "button[data-testid='send-button']",
    "#composer-submit-button",
    "button[aria-label='Send prompt']",
    "button[aria-label*='Send']",
    "button[aria-label*='ارسال']",
]

LIMIT_PATTERNS = [
    r"you['’]ve (hit|reached) (your|the)[^.\n]{0,60}limit",
    r"usage (cap|limit)",
    r"too many requests",
    r"limit[^.\n]{0,40}reset",
    r"try again (later|after|in)",
    r"به سقف",
    r"محدودیت استفاده",
]


class ChatGPTError(RuntimeError):
    """خطای عمومی ChatGPT."""


class ChatGPTLimit(ChatGPTError):
    """سقف استفاده یا 429 روی تولید پاسخ — ۳۰ دقیقه کنار گذاشته شود."""


class ChatGPTLoginRequired(ChatGPTError):
    """کاربر وارد حساب نیست."""


class ChatGPTNoAnswer(ChatGPTError):
    """پاسخی در زمان مجاز نیامد — تلاش دوباره / ایجنت بعدی."""


# علامت زدن تمام پیام‌های فعلی دستیار قبل از ارسال پرامپت جدید
JS_MARK_OLD = r"""
() => {
  document.querySelectorAll('[data-geta-old]').forEach(e => e.removeAttribute('data-geta-old'));
  document.querySelectorAll('[data-geta-last]').forEach(e => e.removeAttribute('data-geta-last'));
  const sel = [
    '[data-chatgpt-search-unit-key$=":assistant"]',
    '[data-content-search-unit-key$=":assistant"]',
    '[data-message-author-role="assistant"]',
    'article[data-turn="assistant"]',
    '[data-turn="assistant"]',
    '[data-dil-message-id]',
    '.DilResponseRoot-HfQrEh',
    '[data-markdown-text-style="assistant-message"]'
  ].join(', ');
  const nodes = [...document.querySelectorAll(sel)];
  nodes.forEach(e => e.setAttribute('data-geta-old', '1'));
  // همچنین هر بلوک دارای h4[data-conversation-role="assistant"]
  document.querySelectorAll('h4[data-conversation-role="assistant"]').forEach(h => {
    const box = h.closest('[data-chatgpt-search-unit-key$=":assistant"]') || h.closest('.block-BQZwFn') || h.parentElement;
    if (box) box.setAttribute('data-geta-old', '1');
  });
  window.__getaClip = null;
  return nodes.length;
}
"""

JS_STATE = r"""
() => {
  const vis = (el) => {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' &&
           s.display !== 'none' && parseFloat(s.opacity || '1') > 0.05;
  };
  const isUserEl = (el) => {
    if (!el) return true;
    if (el.closest('[data-user-message-bubble], [data-chatgpt-search-unit-key$=":user"], [data-content-search-unit-key$=":user"], [data-message-author-role="user"], [data-turn="user"], form')) {
      return true;
    }
    return false;
  };
  const isOld = (el) => !el || el.hasAttribute('data-geta-old') || !!el.closest('[data-geta-old]');
  const cleanFresh = (els) => els.filter(e => !isOld(e) && !isUserEl(e));

  const main = document.querySelector('main') || document.body;
  const counts = {};
  const picks = [];

  // روش ۰ (UI جدید Codex / ChatGPT): واحد پیام دستیار
  const u0 = [...document.querySelectorAll('[data-chatgpt-search-unit-key$=":assistant"], [data-content-search-unit-key$=":assistant"]')];
  const u0Top = u0.filter(e => !u0.some(o => o !== e && o.contains(e)));
  counts.unit_assistant = u0Top.length;
  if (cleanFresh(u0Top).length) picks.push(['unit_assistant', cleanFresh(u0Top)]);

  // روش ۱: بلوک‌های پاسخ DilRenderer / assistant-message
  const u1 = [...document.querySelectorAll('[data-dil-message-id], .DilResponseRoot-HfQrEh, [data-markdown-text-style="assistant-message"]')];
  const u1Top = u1.filter(e => !u1.some(o => o !== e && o.contains(e)));
  counts.dil = u1Top.length;
  if (cleanFresh(u1Top).length) picks.push(['dil', cleanFresh(u1Top)]);

  // روش ۲: h4[data-conversation-role="assistant"]
  const convAnchors = [...document.querySelectorAll('h4[data-conversation-role="assistant"], [data-conversation-role="assistant"]')];
  const convBlocks = [...new Set(convAnchors.map(a =>
      a.closest('[data-chatgpt-search-unit-key$=":assistant"]') ||
      a.closest('.block-BQZwFn') ||
      a.parentElement).filter(Boolean))];
  counts.conv_role = convBlocks.length;
  if (cleanFresh(convBlocks).length) picks.push(['conv', cleanFresh(convBlocks)]);

  // روش ۳: نقش نویسنده (UI کلاسیک)
  const r1 = [...document.querySelectorAll('[data-message-author-role="assistant"]')];
  counts.role = r1.length;
  if (cleanFresh(r1).length) picks.push(['role', cleanFresh(r1)]);

  // روش ۴: article[data-turn="assistant"]
  const r2 = [...document.querySelectorAll('article[data-turn="assistant"], [data-turn="assistant"]')];
  counts.turn = r2.length;
  if (cleanFresh(r2).length) picks.push(['turn', cleanFresh(r2)]);

  // روش ۵: .markdown یا pre خارج از پیام کاربر
  let r4 = [...main.querySelectorAll('.markdown, pre')].filter(e => !isUserEl(e));
  r4 = r4.filter(e => !r4.some(o => o !== e && o.contains(e)));
  counts.markdown = r4.length;
  if (cleanFresh(r4).length) picks.push(['markdown', cleanFresh(r4)]);

  let strategy = null, list = [], last = null;
  if (picks.length) { [strategy, list] = picks[0]; last = list[list.length - 1]; }
  document.querySelectorAll('[data-geta-last]').forEach(e => e.removeAttribute('data-geta-last'));
  let text = '';
  if (last) {
    last.setAttribute('data-geta-last', '1');
    const root = last.querySelector('[data-markdown-copy="contents"]') ||
                 last.querySelector('.DilResponseRoot-HfQrEh') ||
                 last.querySelector('[data-markdown-text-style="assistant-message"]') ||
                 last.querySelector('.markdown') || last;
    const clone = root.cloneNode(true);
    clone.querySelectorAll('h4, button, svg, [data-markdown-copy="exclude"], [data-user-message-bubble]').forEach(e => e.remove());
    text = (clone.innerText || clone.textContent || '').trim();
  }

  const turnEl = last ? last.closest('[data-talvt-turn-state]') : null;
  const turnState = turnEl ? (turnEl.getAttribute('data-talvt-turn-state') || '') : '';
  const turnDone = turnState === 'complete';
  const turnInProgress = turnState === 'in_progress' || !!document.querySelector('[data-talvt-turn-state="in_progress"]');
  const streamingDom = !!(last && last.querySelector('[data-d-streaming-reveal], [data-d-stream-run], .AdaptiveStreaming-uuYOZc'));

  const scroller = document.querySelector('[data-app-action-timeline-scroll], .thread-scroll-container');
  if (scroller && (turnInProgress || streamingDom)) {
    try { scroller.scrollTop = scroller.scrollHeight; } catch (e) {}
  }

  const comp = document.querySelector('div[contenteditable="true"][aria-label="Ask ChatGPT"]') ||
               document.querySelector('#prompt-textarea') ||
               document.querySelector('form div[contenteditable="true"], form textarea');
  let form = comp ? (comp.closest('form') || comp.closest('[data-composer-layout]')) : document.querySelector('form[data-chatgpt-composer]');
  const btns = form ? [...form.querySelectorAll('button')] : [...document.querySelectorAll('button')];
  const label = (b) => ((b.getAttribute('aria-label') || '') + ' ' + (b.getAttribute('data-testid') || '')).toLowerCase();

  const stopBtn = document.querySelector('button[aria-label="Stop"], button[data-testid="stop-button"]') ||
                  btns.find(b => b.getAttribute('data-testid') === 'stop-button' || b.getAttribute('aria-label') === 'Stop') ||
                  btns.find(b => /\bstop\b|توقف/.test(label(b)) && !/dictat|voice|record|صدا|صوت/.test(label(b)));
  const idleBtn = btns.find(b => /send-button|composer-submit|send prompt|speech|voice|dictate|ارسال/.test(label(b)) && !/stop/.test(label(b)));
  const stopVisible = vis(stopBtn);
  const idleVisible = !stopVisible && vis(idleBtn);

  // دکمه‌ی Copy مخصوص پاسخ دستیار (هرگز دکمه‌ی Copy message پیام کاربر انتخاب نشود)
  let copyFound = false;
  if (last) {
    const turnRoot = last.closest('[data-turn-key], [data-content-search-turn-key], article, [data-testid^="conversation-turn"]') || last.parentElement;
    const cb = turnRoot && (
      turnRoot.querySelector('button[data-testid="copy-turn-action-button"]') ||
      turnRoot.querySelector('.actionRow-nqqZbm button[aria-label="Copy"]')
    );
    copyFound = !!cb;
  }

  // اگر دکمه‌ی Continue generating ظاهر شده، خودکار کلیکش کن
  const contBtn = [...document.querySelectorAll('button')].find(b =>
      vis(b) && /continue generating|keep generating|ادامه/i.test((b.innerText || b.getAttribute('aria-label') || '').trim()));
  let continueClicked = false;
  if (contBtn) {
    try { contBtn.click(); continueClicked = true; } catch (e) {}
  }

  const alerts = [...document.querySelectorAll('[role="alert"], [data-sonner-toast]')]
      .map(e => e.innerText || '').join('\n').slice(0, 1500);

  return {
    strategy, counts, fresh: list.length, text, len: text.length,
    turnState, turnDone, turnInProgress, streamingDom,
    stopVisible, stopGlobal: document.querySelectorAll('button[data-testid="stop-button"], button[aria-label="Stop"]').length,
    idleVisible, copyFound, continueClicked, alerts,
    hasComposer: !!comp, hasForm: !!form,
    login: !!document.querySelector('[data-testid="login-button"]') || location.pathname.startsWith('/auth'),
    url: location.href,
  };
}
"""

JS_HOOK_CLIPBOARD = r"""
() => {
  window.__getaClip = null;
  if (window.__getaHooked) return true;
  try {
    const cb = navigator.clipboard;
    if (cb) {
      cb.writeText = async (t) => { window.__getaClip = String(t); };
      cb.write = async (items) => {
        try {
          for (const it of items) {
            if (it.types && it.types.includes('text/plain')) {
              const b = await it.getType('text/plain');
              window.__getaClip = await b.text();
            }
          }
        } catch (e) {}
      };
    }
    window.__getaHooked = true;
    return true;
  } catch (e) { return false; }
}
"""

JS_CLICK_COPY = r"""
() => {
  const last = document.querySelector('[data-geta-last="1"]');
  if (!last) return false;
  const turnRoot = last.closest('[data-turn-key], [data-content-search-turn-key], article, [data-testid^="conversation-turn"]') || last.parentElement;
  let btn = turnRoot && (
    turnRoot.querySelector('button[data-testid="copy-turn-action-button"]') ||
    turnRoot.querySelector('.actionRow-nqqZbm button[aria-label="Copy"]')
  );
  if (!btn) return false;
  btn.scrollIntoView({block: 'center'});
  btn.click();
  return true;
}
"""

JS_MARKDOWN = r"""
() => {
  const el = document.querySelector('[data-geta-last="1"]');
  if (!el) return '';
  const root = el.querySelector('[data-markdown-copy="contents"]') ||
               el.querySelector('.DilResponseRoot-HfQrEh') ||
               el.querySelector('[data-markdown-text-style="assistant-message"]') ||
               el.querySelector('.markdown') || el;

  const rawClone = root.cloneNode(true);
  rawClone.querySelectorAll('h4, button, svg, [data-markdown-copy="exclude"], [data-user-message-bubble]').forEach(e => e.remove());
  const rawInner = (rawClone.innerText || rawClone.textContent || '').trim();
  if ((rawInner.includes('@@@FILE') || rawInner.startsWith('{')) && !root.querySelector('pre')) {
    return rawInner;
  }

  const codeOf = (pre) => {
    const code = pre.querySelector('code');
    let lang = '';
    if (code) { const m = (code.className || '').match(/language-([\w+#.\-]+)/); if (m) lang = m[1]; }
    let text;
    const cm = pre.querySelector('.cm-content');
    if (cm) text = [...cm.querySelectorAll('.cm-line')].map(l => l.textContent).join('\n');
    else if (code) text = code.textContent;
    else text = pre.innerText;
    if (!lang && code) {
      const first = (pre.innerText || '').split('\n')[0].trim();
      if (/^[a-z0-9+#.\- ]{1,20}$/i.test(first) && !text.startsWith(first)) lang = first.toLowerCase().split(' ')[0];
    }
    return {lang, text: (text || '').replace(/\n+$/, '')};
  };

  const walk = (node, depth) => {
    if (node.nodeType === 3) return node.nodeValue;
    if (node.nodeType !== 1) return '';
    const tag = node.tagName.toLowerCase();
    if (['button', 'svg', 'style', 'script'].includes(tag)) return '';
    if (node.getAttribute('data-markdown-copy') === 'exclude' || node.hasAttribute('data-user-message-bubble')) return '';
    if (tag === 'h4' && node.classList && node.classList.contains('sr-only')) return '';
    const inner = () => [...node.childNodes].map(n => walk(n, depth)).join('');
    if (tag === 'pre') {
      const c = codeOf(node);
      const fence = c.text.includes('```') ? '````' : '```';
      return '\n\n' + fence + c.lang + '\n' + c.text + '\n' + fence + '\n\n';
    }
    if (/^h[1-6]$/.test(tag)) return '\n\n' + '#'.repeat(+tag[1]) + ' ' + inner().trim() + '\n\n';
    switch (tag) {
      case 'br': return '\n';
      case 'p': return '\n\n' + inner() + '\n\n';
      case 'strong': case 'b': return '**' + inner() + '**';
      case 'em': case 'i': return '*' + inner() + '*';
      case 'code': return '`' + node.textContent + '`';
      case 'a': return '[' + inner() + '](' + (node.getAttribute('href') || '') + ')';
      case 'hr': return '\n\n---\n\n';
      case 'blockquote': return '\n\n' + inner().trim().split('\n').map(l => '> ' + l).join('\n') + '\n\n';
      default: return inner();
    }
  };
  const out = walk(root, 0).replace(/\n{3,}/g, '\n\n').trim();
  return out.length >= rawInner.length * 0.7 ? out : rawInner;
}
"""

JS_FILL_PROMPT = r"""
(t) => {
  const el = document.querySelector('div.ProseMirror[contenteditable="true"][aria-label="Ask ChatGPT"]') ||
             document.querySelector('div[contenteditable="true"][aria-label="Ask ChatGPT"]') ||
             document.querySelector('#prompt-textarea') ||
             document.querySelector('form div[contenteditable="true"], form textarea');
  if (!el) return -1;
  el.focus();
  if (el.tagName === 'TEXTAREA') {
    const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set;
    setter.call(el, t);
    el.dispatchEvent(new Event('input', {bubbles: true}));
    return el.value.length;
  }
  try {
    const sel = window.getSelection();
    const range = document.createRange();
    range.selectNodeContents(el);
    sel.removeAllRanges();
    sel.addRange(range);
    document.execCommand('delete', false, null);
  } catch (e) {}

  try {
    const dt = new DataTransfer();
    dt.setData('text/plain', t);
    el.dispatchEvent(new ClipboardEvent('paste', {clipboardData: dt, bubbles: true, cancelable: true}));
  } catch (e) {}

  let cur = (el.innerText || '').trim();
  if (cur.length < t.trim().length * 0.8) {
    try {
      document.execCommand('insertText', false, t);
    } catch (e) {}
  }
  return (el.innerText || '').trim().length;
}
"""

JS_CLEAR_COMPOSER = r"""
() => {
  const el = document.querySelector('div.ProseMirror[contenteditable="true"][aria-label="Ask ChatGPT"]') ||
             document.querySelector('div[contenteditable="true"][aria-label="Ask ChatGPT"]') ||
             document.querySelector('#prompt-textarea') ||
             document.querySelector('form div[contenteditable="true"], form textarea');
  if (!el) return;
  el.focus();
  if (el.tagName === 'TEXTAREA') {
    el.value = '';
    el.dispatchEvent(new Event('input', {bubbles: true}));
    return;
  }
  const sel = window.getSelection();
  const range = document.createRange();
  range.selectNodeContents(el);
  sel.removeAllRanges();
  sel.addRange(range);
  document.execCommand('delete', false, null);
}
"""

JS_COMPOSER_LEN = r"""
() => {
  const el = document.querySelector('div.ProseMirror[contenteditable="true"][aria-label="Ask ChatGPT"]') ||
             document.querySelector('div[contenteditable="true"][aria-label="Ask ChatGPT"]') ||
             document.querySelector('#prompt-textarea') ||
             document.querySelector('form div[contenteditable="true"], form textarea');
  if (!el) return -1;
  const raw = (el.tagName === 'TEXTAREA' ? el.value : (el.innerText || '')).trim();
  if (raw === 'Ask ChatGPT') return 0;
  return raw.length;
}
"""


def _safe_log(log: Optional[Callable[[str], None]], msg: str) -> None:
    try:
        (log or print)(msg)
    except Exception:
        print(msg)
    if _devlog is not None:
        _devlog.event("CHATGPT", msg)


def _check_cancel() -> None:
    if _cancel is not None:
        _cancel.check()


def _sleep(sec: float) -> None:
    if _cancel is not None:
        _cancel.sleep(sec)
    else:
        time.sleep(sec)


def _state(page) -> dict:
    try:
        st = page.evaluate(JS_STATE)
        return st if isinstance(st, dict) else {"error": "نتیجه‌ی نامعتبر"}
    except Exception as e:
        return {"error": str(e)[:200]}


def _json_complete(text: str) -> bool:
    s = text.strip()
    if not s.endswith("}"):
        return False
    a = s.find("{")
    if a < 0:
        return False
    try:
        json.loads(s[a:])
        return True
    except Exception:
        return False


def _file_marker_complete(text: str) -> bool:
    s = text.strip()
    if "@@@FILE" in s:
        return s.endswith("@@@END")
    if "<<<FILE" in s:
        return s.endswith("<<<END>>>")
    return True


def _limit_reason(st: dict, hits429: list, waited: float) -> str:
    probe = st.get("alerts") or ""
    text = st.get("text") or ""
    if text and len(text) < 400 and "```" not in text and "{" not in text and "@@@FILE" not in text:
        probe += "\n" + text
    for pat in LIMIT_PATTERNS:
        if re.search(pat, probe, re.IGNORECASE):
            return "پیام سقف استفاده‌ی ChatGPT (limit): " + probe.strip()[:160]
    gen429 = [u for u in hits429 if re.search(r"/backend-api/(f/)?conversation(\?|$|/)", u)]
    if gen429 and not text and waited > 20:
        return "خطای 429 روی تولید پاسخ ChatGPT (limit)"
    return ""


def _first_visible(page, selectors, timeout: float):
    end = time.time() + timeout
    while time.time() < end:
        _check_cancel()
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                if loc.count() and loc.is_visible():
                    return loc
            except Exception:
                pass
        _sleep(0.4)
    return None


def save_debug(page, log=None, reason: str = "") -> Optional[Path]:
    try:
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        base = DEBUG_DIR / f"chatgpt_snap_{stamp}"
        try:
            page.screenshot(path=str(base) + ".png", timeout=15_000)
        except Exception as e:
            _safe_log(log, f"[ChatGPT] عکس صفحه ذخیره نشد: {e}")
        try:
            Path(str(base) + ".html").write_text(page.content(), encoding="utf-8")
        except Exception as e:
            _safe_log(log, f"[ChatGPT] HTML ذخیره نشد: {e}")
        st = _state(page)
        Path(str(base) + ".json").write_text(
            json.dumps({"reason": reason, "state": st}, ensure_ascii=False, indent=2), encoding="utf-8")
        _safe_log(log, f"[ChatGPT] اسنپ‌شات دیباگ در پوشه DEV ذخیره شد: {base.name}")
        return base
    except Exception as e:
        _safe_log(log, f"[ChatGPT] ذخیره‌ی دیباگ ناموفق: {e}")
        return None


def wait_until_idle(page, log=None, timeout: float = 120) -> None:
    start = time.time()
    logged = False
    while time.time() - start < timeout:
        _check_cancel()
        st = _state(page)
        if not st.get("stopVisible") and not st.get("turnInProgress"):
            return
        if not logged:
            _safe_log(log, "[ChatGPT] نوبت قبلی هنوز در حال تولید است؛ صبر برای اتمام آن…")
            logged = True
        _sleep(1.0)
    try:
        stop_btn = page.locator("button[aria-label='Stop'], button[data-testid='stop-button']").first
        if stop_btn.count() and stop_btn.is_visible():
            stop_btn.click(timeout=5_000)
            _sleep(1.5)
    except Exception:
        pass


def open_new_chat(page, log=None) -> None:
    try:
        page.bring_to_front()
    except Exception:
        pass
    page.goto(BASE_URL, wait_until="domcontentloaded", timeout=60_000)
    end = time.time() + 45
    while time.time() < end:
        _check_cancel()
        st = _state(page)
        if st.get("login") and not st.get("hasComposer"):
            raise ChatGPTLoginRequired("وارد حساب ChatGPT نیستی؛ از دکمه‌ی «ورود به ChatGPT» وارد شو.")
        if st.get("hasComposer"):
            return
        _sleep(0.7)
    save_debug(page, log, "composer پیدا نشد")
    raise ChatGPTError("کادر پیام ChatGPT بعد از ۴۵ ثانیه پیدا نشد.")


def send_prompt(page, prompt: str, log=None) -> None:
    wait_until_idle(page, log=log)
    old_count = page.evaluate(JS_MARK_OLD)
    if _devlog is not None:
        _devlog.event("CHATGPT_MARK_OLD", f"تعداد پیام‌های دستیار قبلی که علامت خوردند: {old_count}")

    box = _first_visible(page, COMPOSER_SELECTORS, timeout=30)
    if box is None:
        save_debug(page, log, "composer برای ارسال پیدا نشد")
        raise ChatGPTError("کادر پیام ChatGPT پیدا نشد.")

    box.click(timeout=10_000)
    page.evaluate(JS_CLEAR_COMPOSER)
    _sleep(0.2)

    n = page.evaluate(JS_FILL_PROMPT, prompt)
    if n < len(prompt.strip()) * 0.8:
        page.evaluate(JS_CLEAR_COMPOSER)
        box.click(timeout=5_000)
        page.keyboard.insert_text(prompt)
    _sleep(0.5)

    for attempt in range(3):
        _check_cancel()
        # دفعه‌ی اول: دکمه‌ی ارسال؛ بعدش: Enter روی کادر؛ دفعه‌ی آخر: Ctrl+Enter
        clicked = False
        if attempt == 0:
            for sel in SEND_SELECTORS:
                try:
                    loc = page.locator(sel).first
                    if loc.count() and loc.is_visible() and loc.is_enabled():
                        loc.click(timeout=5_000)
                        clicked = True
                        break
                except Exception:
                    pass
        if not clicked:
            box.click(timeout=5_000)
            page.keyboard.press("Enter" if attempt < 2 else "Control+Enter")

        end = time.time() + 8
        while time.time() < end:
            _check_cancel()
            try:
                rem = page.evaluate(JS_COMPOSER_LEN)
                st = _state(page)
                if rem < 15 and (st.get("stopVisible") or st.get("fresh", 0) > 0 or rem == 0):
                    _safe_log(log, f"[ChatGPT] پرامپت ارسال شد ({len(prompt)} کاراکتر)")
                    return
            except Exception:
                pass
            _sleep(0.4)

        wait_until_idle(page, log=log, timeout=30)
        page.evaluate(JS_MARK_OLD)
    save_debug(page, log, "ارسال پرامپت انجام نشد")
    raise ChatGPTError("پرامپت در ChatGPT ارسال نشد (متن در کادر مونده). لاگ و عکس را بفرست.")


def read_answer(page, plain: str, prompt: str, log=None) -> str:
    """استخراج دقیق و تمیز پاسخ دستیار بدون قاطی شدن با پرامپت کاربر."""
    md = ""
    try:
        md = (page.evaluate(JS_MARKDOWN) or "").strip()
    except Exception as e:
        _safe_log(log, f"[ChatGPT] تبدیل DOM به markdown ناموفق: {e}")

    clip = ""
    try:
        if page.evaluate(JS_HOOK_CLIPBOARD) and page.evaluate(JS_CLICK_COPY):
            end = time.time() + 2.5
            while time.time() < end:
                clip = (page.evaluate("() => window.__getaClip") or "").strip()
                if clip:
                    break
                time.sleep(0.15)
    except Exception:
        pass

    if "ChatGPT said:" in clip:
        clip = clip.split("ChatGPT said:")[-1].strip()

    prompt_head = (prompt or "").strip()[:60]
    clip_is_echo = bool(prompt_head and clip.startswith(prompt_head))
    md_is_echo = bool(prompt_head and md.startswith(prompt_head))

    if _devlog is not None:
        _devlog.event("CHATGPT_READ_CANDIDATES", "مقایسه‌ی خروجی DOM و Copy", {
            "plain_len": len(plain),
            "md_len": len(md),
            "clip_len": len(clip),
            "clip_is_echo": clip_is_echo,
            "md_is_echo": md_is_echo,
            "md_preview": md[:300],
            "clip_preview": clip[:300],
        })

    # اولویت ۱: اگر DOM تمیز و کامل است و اکو ندارد
    if md and not md_is_echo and len(md) >= max(len(plain) * 0.6, 2):
        if "```" in clip and "```" not in md and not clip_is_echo:
            _safe_log(log, f"[ChatGPT] پاسخ از دکمه‌ی Copy خوانده شد ({len(clip)} کاراکتر)")
            return clip
        _safe_log(log, f"[ChatGPT] پاسخ مستقیم از DOM دستیار خوانده شد ({len(md)} کاراکتر)")
        return md

    # اولویت ۲: اگر Copy تمیز است و اکو ندارد
    if clip and not clip_is_echo:
        _safe_log(log, f"[ChatGPT] پاسخ از دکمه‌ی Copy خوانده شد ({len(clip)} کاراکتر)")
        return clip

    return md or plain or clip


def wait_answer(page, prompt: str, log=None, timeout: float = 900, hits429: Optional[list] = None,
                poll: float = 0.9) -> str:
    hits429 = hits429 if hits429 is not None else []
    start = time.time()
    last_text, last_change, last_report = None, start, start
    debug_done = stop_suspect = False
    st: dict = {}

    while True:
        _check_cancel()
        now = time.time()
        waited = now - start
        if waited > timeout:
            save_debug(page, log, "timeout")
            raise ChatGPTNoAnswer(f"ChatGPT در {int(timeout)} ثانیه پاسخ کامل نداد.")

        st = _state(page)
        if "error" in st:
            if now - last_report >= REPORT_EVERY:
                _safe_log(log, f"[ChatGPT] خواندن صفحه ناموفق ({int(waited)}s): {st['error']}")
                last_report = now
            _sleep(poll)
            continue

        if st.get("login"):
            raise ChatGPTLoginRequired("سشن ChatGPT منقضی شده؛ دوباره وارد شو.")
        reason = _limit_reason(st, hits429, waited)
        if reason:
            save_debug(page, log, "rate_limit")
            raise ChatGPTLimit(reason)

        text = st.get("text") or ""
        # اگر به هر دلیلی متن خوانده‌شده دقیقاً همان پرامپت ارسالی بود، آن را نادیده بگیر
        prompt_head = (prompt or "").strip()[:80]
        if prompt_head and len(prompt_head) >= 30 and text.startswith(prompt_head) and "ChatGPT said:" not in text:
            text = ""

        if st.get('continueClicked'):
            _safe_log(log, '[ChatGPT] ↻ دکمه‌ی Continue generating خودکار کلیک شد…')
            last_change = now
        if text != last_text:
            last_text, last_change = text, now
            stop_suspect = False
        stable = now - last_change

        actively_streaming = bool(st.get("stopVisible") or st.get("turnInProgress") or st.get("streamingDom"))
        if actively_streaming and text and stable >= STOP_SUSPECT and not stop_suspect:
            stop_suspect = True
            _safe_log(log, "[ChatGPT] وضعیت تولید بیش از ۲۲ ثانیه ثابت ماند؛ بررسی تکمیل پاسخ…")
        generating = actively_streaming and not stop_suspect
        detectable = (actively_streaming or st.get("idleVisible") or st.get("turnDone")) and not stop_suspect

        if not actively_streaming and stable >= AGENT_IDLE_SECONDS:
            save_debug(page, log, "idle")
            if text.strip():
                _safe_log(log, "[ChatGPT] پاسخ دیگر تغییر نکرد؛ همان متن موجود استفاده می‌شود.")
                return text
            raise AgentTimeout(f"ChatGPT بیش از {AGENT_IDLE_SECONDS // 60} دقیقه پاسخی نداد.")

        if now - last_report >= REPORT_EVERY:
            _safe_log(log, f"[ChatGPT] در انتظار پاسخ… {int(waited)}s | پیام جدید: {st.get('fresh', 0)}"
                           f" | طول متن: {len(text)} | در حال تولید: {'بله' if generating else 'خیر'}"
                           f" | سلکتور: {st.get('strategy') or '-'}")
            last_report = now

        if not text and waited >= DEBUG_AFTER and not debug_done:
            debug_done = True
            save_debug(page, log, f"{DEBUG_AFTER} ثانیه بدون متن")

        if text.strip():
            markers_ok = _file_marker_complete(text)
            turn_done = bool(st.get("turnDone")) and not generating and stable >= 1.8 and markers_ok
            early = (not generating and stable >= STABLE_EARLY and markers_ok
                     and (_json_complete(text) or st.get("copyFound")))
            normal = detectable and not generating and stable >= STABLE_DONE and markers_ok
            blind = not generating and stable >= STABLE_BLIND
            if turn_done or early or normal or blind:
                _safe_log(log, f"[ChatGPT] پاسخ کامل شد ({int(waited)}s، سلکتور: {st.get('strategy')}، طول: {len(text)})")
                return read_answer(page, text, prompt, log)

        _sleep(poll)


def ask(page, prompt: str, log: Optional[Callable[[str], None]] = None,
        timeout: float = 900, new_chat: bool = True) -> str:
    page.set_default_timeout(30_000)
    hits429: list = []

    def on_response(resp):
        try:
            if resp.status == 429 and "/backend-api/" in resp.url:
                hits429.append(resp.url)
        except Exception:
            pass

    page.on("response", on_response)
    try:
        if new_chat:
            _safe_log(log, "[ChatGPT] باز کردن گفتگوی جدید…")
            open_new_chat(page, log)
        else:
            _safe_log(log, "[ChatGPT] ادامه در همان گفتگوی پروژه")
        send_prompt(page, prompt, log)
        return wait_answer(page, prompt, log, timeout, hits429)
    finally:
        try:
            page.remove_listener("response", on_response)
        except Exception:
            pass
