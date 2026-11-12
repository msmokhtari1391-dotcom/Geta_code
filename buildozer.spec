[app]

# ---- اطلاعات برنامه ----
title = Geta Code
package.name = getacode
package.domain = org.getacode
source.dir = .
source.include_exts = py,png,jpg,html,json,txt
source.exclude_dirs = .github,.buildozer,bin,tests,__pycache__,venv,debug,projects,browser_profiles
source.exclude_patterns = *.zip,*.html.bak
version = 1.0.0

# ---- وابستگی‌ها ----
# فقط پکیج‌های واقعی. «android» پکیج pip نیست و حذف شده.
requirements = python3,kivy==2.3.0,pyjnius,certifi

# ---- نمایش ----
orientation = landscape
fullscreen = 0
presplash.filename = %(source.dir)s/assets/presplash.png
android.presplash_color = #0a0a14

# ---- اندروید ----
android.permissions = INTERNET,ACCESS_NETWORK_STATE,WAKE_LOCK
android.api = 33
android.minapi = 24
android.ndk = 25b
android.archs = arm64-v8a
android.allow_backup = True
android.accept_sdk_license = True
android.logcat_filters = *:S python:D

# ---- نسخه‌ی python-for-android ----
# برای جلوگیری از خطای pyjnius==1.7.0 و python 3.14 یک شاخه‌ی پایدار قفل شده
p4a.branch = v2024.01.21

[buildozer]
log_level = 2
warn_on_root = 1
