"""سیگنال توقف ساخت. BuildCancelled از BaseException است تا except Exception آن را نبلعد."""
import threading

STOP = threading.Event()


class BuildCancelled(BaseException):
    pass


def check():
    if STOP.is_set():
        raise BuildCancelled("توسط کاربر متوقف شد")


def sleep(seconds):
    if STOP.wait(max(0.0, seconds)):
        raise BuildCancelled("توسط کاربر متوقف شد")
