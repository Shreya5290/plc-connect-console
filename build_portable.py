"""
PyInstaller build script for Click2Connect ONEFILE EXE
Generates a single standalone executable
"""

import sys
import shutil
import subprocess
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DIST_DIR = BASE_DIR / "dist_portable"
BUILD_DIR = BASE_DIR / "build_portable"
VERSION = (BASE_DIR / "version.txt").read_text().strip()
EXE_NAME = f"Click2Connect_{VERSION}"

print(f"\n{'='*60}")
print(f"  Click2Connect {VERSION} - ONEFILE Build")
print(f"{'='*60}\n")

# Check if PyInstaller is installed
print("[Build] Checking PyInstaller installation...")

result = subprocess.run(
    [sys.executable, "-m", "pip", "show", "pyinstaller"],
    capture_output=True,
    text=True,
)

if result.returncode != 0:
    print("Installing PyInstaller...")
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "pyinstaller"],
        check=False,
    )

# Verify PyInstaller
try:
    import PyInstaller  # noqa: F401
    import PyInstaller.__main__  # noqa: F401
except Exception as e:
    print(f"ERROR: Could not load PyInstaller: {e}")
    sys.exit(1)

# Clean old builds
print("[Build] Cleaning previous builds...")

for d in [DIST_DIR, BUILD_DIR]:
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)
        print(f"  ✓ Removed {d.name}")

# Create required directories
print("[Build] Creating app directories...")

directories = [
    BASE_DIR / "cache",
    BASE_DIR / "cache" / "yaml",
    BASE_DIR / "cache" / "backups",
    BASE_DIR / "logs",
    BASE_DIR / "connectapp" / "migrations",
]

for d in directories:
    d.mkdir(parents=True, exist_ok=True)

# Ensure migrations __init__.py exists
migrations_init = BASE_DIR / "connectapp" / "migrations" / "__init__.py"

if not migrations_init.exists():
    migrations_init.touch()

print("[Build] Generating ONEFILE PyInstaller spec...")

# Only bundle db.sqlite3 when it already exists. The app auto-creates the
# database on first run (see connectapp.views._ensure_app_ready), so a missing
# db.sqlite3 must not break the build.
db_data_entry = ""
if (BASE_DIR / "db.sqlite3").exists():
    db_data_entry = "        ('db.sqlite3', '.'),\r\n"

# Important fix: start the Django server explicitly via server_entrypoint.py
spec_content = f'''# -*- mode: python ; coding: utf-8 -*-\r\n\r\na = Analysis(\r\n    ['server_entrypoint.py'],\r\n    pathex=[],\r\n    binaries=[],\r\n    datas=[\r\n        ('connectapp/templates', 'connectapp/templates'),\r\n        ('connectapp/migrations', 'connectapp/migrations'),\r\n        ('cache', 'cache'),\r\n        ('logs', 'logs'),\r\n        ('version.txt', '.'),\r\n{db_data_entry}    ],\r\n    hiddenimports=[\r\n        'django',\r\n        'django.conf',\r\n        'django.core',\r\n        'django.core.management',\r\n        'django.core.management.commands.runserver',\r\n        'django.db',\r\n        'django.db.backends.sqlite3',\r\n        'django.contrib.admin',\r\n        'django.contrib.auth',\r\n        'django.contrib.contenttypes',\r\n        'django.contrib.sessions',\r\n        'django.contrib.messages',\r\n        'django.contrib.staticfiles',\r\n        'connectapp',\r\n        'connectapp.views',\r\n        'connectapp.forms',\r\n        'connectapp.urls',\r\n        'pycomm3',\r\n        'pymodbus',\r\n        'snap7',\r\n        'opcua',\r\n        'asyncio',\r\n        'threading',\r\n        'json',\r\n        'yaml',\r\n    ],\r\n    hookspath=[],\r\n    runtime_hooks=[],\r\n    excludedimports=[],\r\n    noarchive=False,\r\n)\r\n\r\npyz = PYZ(a.pure)\r\n\r\nexe = EXE(\r\n    pyz,\r\n    a.scripts,\r\n    a.binaries,\r\n    a.zipfiles,\r\n    a.datas,\r\n    [],\r\n    name='{EXE_NAME}',\r\n    debug=False,\r\n    bootloader_ignore_signals=False,\r\n    strip=False,\r\n    upx=False,\r\n    console=True,\r\n    disable_windowed_traceback=False,\r\n    icon=['click2connect_icon.ico'],\r\n)\r\n'''

spec_file = BASE_DIR / f"Click2Connect_{VERSION}_portable.spec"
spec_file.write_text(spec_content, encoding="utf-8")

print(f"  ✓ {spec_file.name}")

print("\n[Build] Compiling ONEFILE executable...")
print(f"  Building: {EXE_NAME}.exe\n")

import PyInstaller.__main__

PyInstaller.__main__.run(
    [
        str(spec_file),
        "--noconfirm",
        "--distpath",
        str(DIST_DIR),
        "--workpath",
        str(BUILD_DIR),
    ]
)

# Expected output for onefile build
exe_path = DIST_DIR / f"{EXE_NAME}.exe"

if exe_path.exists():
    print(f"\n{'='*60}")
    print("  ✓ BUILD SUCCESSFUL!")
    print(f"{'='*60}\n")

    print("📦 EXE LOCATION:")
    print(f"   {exe_path}\n")

    print("📋 DEPLOYMENT:")
    print("   1. Copy {0}".format(exe_path.name))
    print("   2. Send it to the target PC")
    print("   3. Double-click the EXE")
    print("   4. Wait a few seconds for startup")
    print("   5. Open http://localhost:8000 manually if needed\n")

    print("✅ Single-file EXE")
    print("✅ No _internal folder")
    print("✅ No Python required on target PC")
else:
    # Don't fail hard if PyInstaller produced a different path;
    # print directory listing to help diagnose.
    print(f"\n❌ BUILD FAILED - EXE NOT FOUND at: {exe_path}")
    try:
        print("Existing files in dist_portable:")
        for p in sorted(DIST_DIR.glob("*")):
            print("  -", p.name)
    except Exception:
        pass
    sys.exit(1)

