# -*- mode: python ; coding: utf-8 -*-

a = Analysis(
    ['server_entrypoint.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('connectapp/templates', 'connectapp/templates'),
        ('connectapp/migrations', 'connectapp/migrations'),
        ('cache', 'cache'),
        ('logs', 'logs'),
        ('version.txt', '.'),
    ],
    hiddenimports=[
        'django',
        'django.conf',
        'django.core',
        'django.core.management',
        'django.core.management.commands.runserver',
        'django.db',
        'django.db.backends.sqlite3',
        'django.contrib.admin',
        'django.contrib.auth',
        'django.contrib.contenttypes',
        'django.contrib.sessions',
        'django.contrib.messages',
        'django.contrib.staticfiles',
        'connectapp',
        'connectapp.views',
        'connectapp.forms',
        'connectapp.urls',
        'pycomm3',
        'pymodbus',
        'snap7',
        'opcua',
        'asyncio',
        'threading',
        'json',
        'yaml',
    ],
    hookspath=[],
    runtime_hooks=[],
    excludedimports=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='Click2Connect_vV1.0',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    icon=['click2connect_icon.ico'],
)
