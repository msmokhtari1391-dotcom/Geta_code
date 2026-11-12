[app]
title = Geta Code
package.name = getacode
package.domain = org.geta
source.dir = .
source.include_exts = py,png,jpg,kv,atlas,html,css,js,json,webp,txt
source.exclude_dirs = tests,bin,.git,.github,.geta,__pycache__,DEV,projects,browser_profiles

version = 1.0.0

requirements = python3==3.11.5,hostpython3==3.11.5,kivy==2.3.0,pyjnius,android,certifi,urllib3

icon.filename = %(source.dir)s/assets/icon.png
presplash.filename = %(source.dir)s/assets/presplash.png
presplash.color = #0a0405

orientation = landscape
fullscreen = 1

android.permissions = INTERNET,ACCESS_NETWORK_STATE,WAKE_LOCK,WRITE_EXTERNAL_STORAGE,READ_EXTERNAL_STORAGE
android.api = 33
android.minapi = 24
android.ndk = 25b
android.archs = arm64-v8a
android.allow_backup = True
android.accept_sdk_license = True

android.manifest.application_attrs = android:usesCleartextTraffic="true"
android.manifest.activity_attrs = android:screenOrientation="sensorLandscape" android:windowSoftInputMode="adjustPan" android:configChanges="orientation|screenSize|screenLayout|keyboardHidden"

[buildozer]
log_level = 2
warn_on_root = 0
