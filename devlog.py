# -*- coding: utf-8 -*-
"""
devlog.py — سیستم ثبت کامل وقایع فنی و دیباگ خودکار در پوشه‌ی DEV/
هر بار که برنامه باز می‌شود یا ساختی اجرا می‌شود، تمام اتفاقات (اتصال به کروم، سلکتورهای ChatGPT،
متن کامل پرامپت‌ها، پاسخ خام ایجنت‌ها، پاسخ OpenRouter، خروجی استخراج‌شده و Traceback خطاها)
به صورت لحظه‌ای در پوشه‌ی DEV ذخیره می‌شود.
"""
import json
import os
import time
import traceback
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent
DEV_DIR = ROOT_DIR / "DEV"

_SESSION_ID = time.strftime("%Y%m%d_%H%M%S")
_COUNTER = 0


def ensure_dev() -> Path:
    DEV_DIR.mkdir(parents=True, exist_ok=True)
    return DEV_DIR


def set_session(name: str = "") -> str:
    global _SESSION_ID, _COUNTER
    slug = "".join(c if c.isalnum() or c in "-_" else "_" for c in (name or "session"))[:30].strip("_") or "session"
    _SESSION_ID = f"{time.strftime('%Y%m%d_%H%M%S')}_{slug}"
    _COUNTER = 0
    ensure_dev()
    event("SESSION_START", f"شروع سشن جدید: {_SESSION_ID}")
    return _SESSION_ID


def log_file() -> Path:
    ensure_dev()
    return DEV_DIR / f"dev_{_SESSION_ID}.log"


def latest_report_file() -> Path:
    ensure_dev()
    return DEV_DIR / "LATEST_DEV_REPORT.txt"


def _append(text: str):
    try:
        ensure_dev()
        with log_file().open("a", encoding="utf-8") as f:
            f.write(text)
        with latest_report_file().open("a", encoding="utf-8") as f:
            f.write(text)
    except Exception:
        pass


def reset_latest(title: str = ""):
    try:
        ensure_dev()
        header = (
            f"====================================================================\n"
            f"GETA CODE — گزارش کامل دیباگ و دولوپ (DEV REPORT)\n"
            f"زمان شروع: {time.strftime('%Y-%m-%d %H:%M:%S')} | عنوان: {title}\n"
            f"====================================================================\n\n"
        )
        latest_report_file().write_text(header, encoding="utf-8")
        log_file().write_text(header, encoding="utf-8")
    except Exception:
        pass


def event(category: str, message: str, data=None):
    """ثبت یک رویداد با دسته‌بندی مشخص در فایل DEV."""
    ts = time.strftime("%H:%M:%S")
    line = f"[{ts}] [{category}] {message}\n"
    if data is not None:
        try:
            if isinstance(data, (dict, list)):
                dump = json.dumps(data, ensure_ascii=False, indent=2)
            else:
                dump = str(data)
            if len(dump) > 15000:
                dump = dump[:15000] + f"\n... [ادامه در فایل جداگانه، کل طول: {len(dump)}]"
            line += f"--- DATA ---\n{dump}\n------------\n"
        except Exception as e:
            line += f"--- DATA ERROR: {e} ---\n"
    _append(line)


def record_exchange(role: str, provider: str, stage: str, prompt: str, raw_reply: str,
                    cleaned_reply: str = "", parsed_info: str = "", extra: dict = None):
    """ذخیره‌ی کامل یک رفت‌وبرگشت با ایجنت یا OpenRouter در یک فایل مجزا داخل DEV و ثبت در گزارش اصلی."""
    global _COUNTER
    _COUNTER += 1
    try:
        ensure_dev()
        slug = "".join(c if c.isalnum() or c in "-_" else "_" for c in f"{role}_{provider}_{stage}")[:50].strip("_")
        fname = f"{_SESSION_ID}_{_COUNTER:02d}_{slug}.txt"
        fpath = DEV_DIR / fname
        body = [
            f"============================================================",
            f"گام: {_COUNTER} | زمان: {time.strftime('%Y-%m-%d %H:%M:%S')}",
            f"نقش (Role): {role}",
            f"سرویس (Provider): {provider}",
            f"مرحله (Stage): {stage}",
            f"طول پرامپت: {len(prompt or '')} کاراکتر",
            f"طول پاسخ خام: {len(raw_reply or '')} کاراکتر",
            f"طول پاسخ تمیزشده: {len(cleaned_reply or '')} کاراکتر",
        ]
        if extra:
            body.append(f"اطلاعات تکمیلی: {json.dumps(extra, ensure_ascii=False)}")
        if parsed_info:
            body.append(f"نتیجه‌ی استخراج (Parsed): {parsed_info}")
        body.extend([
            f"============================================================",
            f"\n>>> [1] PROMPT SENT <<<\n{prompt or '(خالی)'}\n",
            f"\n>>> [2] RAW REPLY RECEIVED <<<\n{raw_reply or '(خالی)'}\n",
        ])
        if cleaned_reply and cleaned_reply != raw_reply:
            body.append(f"\n>>> [3] CLEANED REPLY (AFTER STRIP_ECHO) <<<\n{cleaned_reply}\n")
        fpath.write_text("\n".join(body), encoding="utf-8")

        summary = (
            f"گام #{_COUNTER} | نقش={role} | سرویس={provider} | مرحله={stage} | "
            f"پرامپت={len(prompt or '')}ch | پاسخ خام={len(raw_reply or '')}ch | "
            f"پاسخ تمیز={len(cleaned_reply or raw_reply or '')}ch | فایل={fname}"
        )
        if parsed_info:
            summary += f"\n    نتیجه استخراج: {parsed_info}"
        preview = (cleaned_reply or raw_reply or "").strip()[:500].replace("\n", " ⏎ ")
        if preview:
            summary += f"\n    پیش‌نمایش پاسخ: {preview}"
        event("AI_EXCHANGE", summary)
    except Exception as e:
        event("DEVLOG_ERROR", f"خطا در ذخیره‌ی تبادل: {e}")


def error(where: str, exc: BaseException = None, message: str = "", extra_data=None):
    """ثبت کامل خطا به همراه Stack Trace دقیق در پوشه‌ی DEV."""
    tb = traceback.format_exc() if exc is not None else ""
    msg = f"خطا در {where}: {message or exc}"
    payload = {"error": str(exc or message), "traceback": tb}
    if extra_data is not None:
        payload["extra"] = extra_data
    event("ERROR", msg, payload)
