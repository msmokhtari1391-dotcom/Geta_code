"""جلوگیری از خاموش شدن صفحه هنگام ساخت در اندروید و ویندوز."""
_wake_lock = None

def keep_awake(on=True):
    global _wake_lock
    try:
        from jnius import autoclass
        PythonActivity = autoclass("org.kivy.android.PythonActivity")
        Context = autoclass("android.content.Context")
        PowerManager = autoclass("android.os.PowerManager")
        activity = PythonActivity.mActivity
        if activity is None:
            return False
        if on:
            if _wake_lock is None:
                pm = activity.getSystemService(Context.POWER_SERVICE)
                _wake_lock = pm.newWakeLock(PowerManager.SCREEN_BRIGHT_WAKE_LOCK | PowerManager.ON_AFTER_RELEASE, "GetaCode::BuildLock")
            if not _wake_lock.isHeld():
                _wake_lock.acquire()
        else:
            if _wake_lock is not None and _wake_lock.isHeld():
                _wake_lock.release()
        return True
    except Exception:
        return False

def release():
    keep_awake(False)
