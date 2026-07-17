#!/usr/bin/env python
"""
Click2Connect Portable Build
More robust version that handles PyInstaller installation better
"""
import os
import sys
import subprocess
import shutil
from pathlib import Path

def run_command(cmd, description, fatal=True):
    """Run a command and handle errors."""
    try:
        print(f"[Build] {description}...")
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        if result.returncode != 0 and fatal:
            print(f"ERROR: {description} failed")
            print(result.stderr)
            sys.exit(1)
        if result.stdout:
            print(f"  {result.stdout.strip()}")
        return result.returncode == 0
    except Exception as e:
        if fatal:
            print(f"ERROR: {e}")
            sys.exit(1)
        return False

def main():
    BASE_DIR = Path(__file__).resolve().parent
    DIST_DIR = BASE_DIR / 'dist_portable'
    BUILD_DIR = BASE_DIR / 'build_portable'
    VERSION = (BASE_DIR / 'version.txt').read_text().strip()
    
    print(f"\n{'='*60}")
    print(f"  Click2Connect v{VERSION} - Building Portable EXE")
    print(f"{'='*60}\n")
    
    # Step 1: Verify Python
    print(f"[1/6] Python version: {sys.version.split()[0]}")
    
    # Step 2: Install PyInstaller
    print(f"\n[2/6] Installing PyInstaller...")
    run_command(
        f'"{sys.executable}" -m pip install pyinstaller -q',
        "PyInstaller installation",
        fatal=False
    )
    # Try to import to verify
    try:
        import PyInstaller.main
        print("  ✓ PyInstaller ready")
    except ImportError:
        print("  WARNING: Trying pip install again...")
        run_command(f'"{sys.executable}" -m pip install pyinstaller', "PyInstaller (verbose)")
    
    # Step 3: Create directories
    print(f"\n[3/6] Creating app directories...")
    required_dirs = [
        BASE_DIR / 'cache',
        BASE_DIR / 'cache' / 'yaml',
        BASE_DIR / 'cache' / 'backups',
        BASE_DIR / 'logs',
        BASE_DIR / 'connectapp' / 'migrations',
    ]
    for d in required_dirs:
        d.mkdir(parents=True, exist_ok=True)
    print(f"  ✓ {len(required_dirs)} directories created")
    
    # Step 4: Create __init__.py
    print(f"\n[4/6] Creating migrations __init__.py...")
    init_file = BASE_DIR / 'connectapp' / 'migrations' / '__init__.py'
    init_file.touch()
    print(f"  ✓ Created")
    
    # Step 5: Clean old builds
    print(f"\n[5/6] Cleaning previous builds...")
    for d in [DIST_DIR, BUILD_DIR]:
        if d.exists():
            shutil.rmtree(d)
    print(f"  ✓ Cleaned")
    
    # Create spec file
    spec_file = BASE_DIR / f'Click2Connect_v{VERSION}_portable.spec'
    spec_content = f'''# -*- mode: python ; coding: utf-8 -*-
a = Analysis(
    ['manage.py'],
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
)

pyz = PYZ(a.pure, a.zipped_data, cipher=None)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='Click2Connect_v{VERSION}',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    onefile=True,
)
'''
    
    spec_file.write_text(spec_content)
    print(f"  Spec file created: {spec_file.name}")
    
    # Step 6: Run PyInstaller
    print(f"\n[6/6] Building portable EXE (2-5 minutes)...")
    spec_file = BASE_DIR / f'Click2Connect_v{VERSION}_portable.spec'
    
    cmd = f'"{sys.executable}" -m PyInstaller "{spec_file}" --distpath "{DIST_DIR}" --workpath "{BUILD_DIR}"'
    result = subprocess.run(cmd, shell=True, capture_output=False, text=True)
    
    if result.returncode == 0:
        exe_path = DIST_DIR / f'Click2Connect_v{VERSION}' / f'Click2Connect_v{VERSION}.exe'
        if exe_path.exists():
            print(f"\n{'='*60}")
            print(f"  ✓ BUILD SUCCESSFUL!")
            print(f"{'='*60}\n")
            print(f"📦 EXE Location:")
            print(f"   {exe_path}\n")
            print(f"📋 To Deploy:")
            print(f"   1. Copy: {DIST_DIR / f'Click2Connect_v{VERSION}'}/")
            print(f"   2. Extract on target system")
            print(f"   3. Run: Click2Connect_v{VERSION}.exe\n")
        else:
            print(f"\n❌ Build completed but EXE not found at expected location")
            sys.exit(1)
    else:
        print(f"\n❌ Build failed")
        sys.exit(1)

if __name__ == '__main__':
    main()
