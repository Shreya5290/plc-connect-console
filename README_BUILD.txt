╔════════════════════════════════════════════════════════════════╗
║        Click2Connect v2026.07.10 - Portable Build Setup         ║
╚════════════════════════════════════════════════════════════════╝

✅ EVERYTHING IS READY!

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

📋 TO BUILD PORTABLE EXE (2 STEPS):

1️⃣  Double-click: SETUP.bat
    (Installs PyInstaller - one time only)

2️⃣  Double-click: BUILD.bat
    (Creates portable EXE - 2-5 minutes)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

📦 DEPLOYMENT TO OTHER SYSTEMS:

After BUILD.bat completes:

1. Open: dist_portable\Click2Connect_v2026.07.10\
2. Copy entire folder to USB/Cloud/Network
3. Extract on target system (any folder)
4. Double-click: Click2Connect_v2026.07.10.exe
5. Browser opens automatically to: http://localhost:8000

✅ NO INSTALLATION NEEDED!
✅ NO PYTHON NEEDED!
✅ Database auto-created!

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

📚 FILES IN THIS FOLDER:

SETUP.bat                 - Install PyInstaller (run first)
BUILD.bat                 - Build the EXE (run second)
build_portable.py         - Build configuration (auto-called by BUILD.bat)
DEPLOYMENT_GUIDE.md       - Detailed deployment instructions
version.txt               - App version (edit to update)
requirements.txt          - Python dependencies

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

🔧 COMMAND LINE (MANUAL):

# Install dependencies
pip install -r requirements.txt
pip install pyinstaller

# Build portable EXE
python build_portable.py

# Find built EXE at:
# dist_portable\Click2Connect_v2026.07.10\Click2Connect_v2026.07.10.exe

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

✨ AUTO-FEATURES ON TARGET SYSTEM:

✓ Creates cache/ folder automatically
✓ Creates logs/ folder automatically  
✓ Creates database automatically
✓ Runs migrations automatically
✓ No configuration needed
✓ Just run .exe and use!

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Questions? See: DEPLOYMENT_GUIDE.md

Ready? Start with: SETUP.bat
