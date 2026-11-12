"""جلوگیری از خاموش شدن صفحه و خواب ویندوز هنگام ساخت (فقط ویندوز)."""
import os

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002


def keep_awake(on=True):
    """باید در همون thread کاری صدا زده بشه؛ Windows این وضعیت رو برای thread فعلی نگه می‌داره."""
    if os.name != "nt":
        return False
    try:
        import ctypes
        flags = ES_CONTINUOUS | ((ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED) if on else 0)
        ctypes.windll.kernel32.SetThreadExecutionState(flags)
        return True
    except Exception:
        return False


def release():
    keep_awake(False)
