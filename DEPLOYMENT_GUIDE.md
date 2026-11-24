# Click2Connect v2026.07.10 - Portable Deployment Guide

## 🚀 Building the Portable EXE

### Prerequisites
```powershell
pip install -r requirements.txt
pip install pyinstaller
```

### Build Steps

1. **Navigate to project directory:**
```powershell
cd C:\Users\a00587102\plc_connect
```

2. **Run the build script:**
```powershell
python build_portable.py
```

3. **Wait for completion (2-5 minutes)**
   - You'll see compilation progress
   - Final location will be displayed

4. **Built files location:**
```
C:\Users\a00587102\plc_connect\dist_portable\Click2Connect_v2026.07.10\
```

---

## 📦 Deployment to Other Systems

### On Source System (Your PC)
1. Open `dist_portable/` folder
2. Copy entire `Click2Connect_v2026.07.10/` folder
3. Paste to USB drive or cloud storage

### On Target System (Other PC)
1. **Paste the entire folder** anywhere on the target system
   - No installation needed
   - No Python installation needed
   - Works on Windows 10/11 64-bit

2. **Double-click:**
   - `Click2Connect_v2026.07.10.exe`

3. **Wait 10 seconds** for server to start

4. **Browser will open automatically** to:
   ```
   http://localhost:8000
   ```

5. **Click2Connect remains in the Windows notification area**
   - Double-click the tray icon or choose **Open Click2Connect** to reopen the UI
   - Choose **Exit Click2Connect** from the tray menu to stop the application

---

## ✅ What's Automatic

On first run, the app will automatically:
- ✅ Create `cache/`, `cache/yaml/`, `cache/backups/` folders
- ✅ Create `logs/` folder  
- ✅ Create `db.sqlite3` database
- ✅ Run Django migrations
- ✅ Initialize all configurations

**No manual steps needed!**

---

## Microsoft SQL Server Data Logging (Optional)

The bridge can log every successfully handled PLC value without delaying PLC/SCADA communication. Logging is disabled by default.

1. Install **Microsoft ODBC Driver 18 for SQL Server** on the computer running Click2Connect.
2. Give the SQL user permission to connect to the database and create/insert into tables.
3. Open the required **Connection** tab and find **Microsoft SQL Server Logging** below the mapping table.
4. Enter the server, database, authentication, ODBC driver, and table name. Use **Test Connection** to verify access.
5. Enable logging and select **Save SQL Settings**. Settings are stored independently for each connection slot.

The saved password is never displayed in the UI. Leave the password field blank when editing other settings to retain the existing password.

For unattended deployments, the following environment variables remain available as fallback defaults. Set them before starting Click2Connect:

```powershell
setx C2C_SQL_ENABLED "true"
setx C2C_SQL_CONNECTION_STRING "DRIVER={ODBC Driver 18 for SQL Server};SERVER=SQLSERVER;DATABASE=Click2Connect;Trusted_Connection=yes;Encrypt=yes;TrustServerCertificate=yes"
setx C2C_SQL_TABLE "PlcDataLog"
```

The table is created automatically on the first write. Each row includes the timestamp, bridge slot, PLC brand and connection, tag or Modbus address, OPC UA node ID, PLC value, SCADA value, and configured data type.

Optional tuning variables are `C2C_SQL_BATCH_SIZE` (default `250`) and `C2C_SQL_QUEUE_SIZE` (default `10000`). If SQL Server is unavailable, the independent logger retries while the bridge continues running. Once the bounded queue is full, new log rows are dropped rather than interrupting PLC communication.

To disable logging:

```powershell
setx C2C_SQL_ENABLED "false"
```

---

## 📋 File Structure in Portable Build

```
Click2Connect_v2026.07.10/
├── Click2Connect_v2026.07.10.exe      (Main executable)
├── _internal/                          (Dependencies - auto-generated)
│   ├── python313.dll
│   ├── Django/
│   ├── pycomm3/
│   ├── pymodbus/
│   ├── opcua/
│   └── ...
├── cache/                              (Auto-created on first run)
│   ├── yaml/
│   └── backups/
├── logs/                               (Auto-created on first run)
├── db.sqlite3                          (Auto-created on first run)
└── connectapp/                         (Bundled app files)
```

---

## 🔧 Manual Control (Advanced)

### Start Server Manually
```powershell
Click2Connect_v2026.07.10.exe runserver 0.0.0.0:8000
```

### Access from Another Computer on Network
```
http://<YOUR_PC_IP>:8000
```

### Database Backup
- Database stored in: `db.sqlite3` (same folder as EXE)
- Backup entire folder to preserve data
- Cache stored in: `cache/landing_data.json`

---

## 🐛 Troubleshooting

### Port Already in Use
If port 8000 is in use:
```powershell
Click2Connect_v2026.07.10.exe runserver 0.0.0.0:8001
```
Then access: `http://localhost:8001`

### Database Issues
If you get migration errors:
1. Delete `db.sqlite3`
2. Restart the EXE
3. Database will be recreated

### Permissions Error
- Run as Administrator if needed
- Ensure write permissions to folder location

### PLC Connection Issues
- Verify PLC is on same network
- Check IP address in app
- Verify firewall allows connections

---

## 📊 System Requirements

**Target System Requirements:**
- Windows 10/11 64-bit
- ~300MB free disk space
- .NET Framework (usually pre-installed)
- Network access (for PLC connection)

**NOT required:**
- Python installation
- Any development tools
- Administrator access (usually)

---

## 🔄 Updates

To update to new version:
1. Build new portable EXE with updated code
2. Copy new folder to systems
3. Old data remains if in same folder location
4. Or manually copy `db.sqlite3` to new folder to preserve data

---

## 💾 Backing Up Data

**Backup these files from portable folder:**
- `db.sqlite3` - Application database
- `cache/landing_data.json` - Configuration cache
- `cache/backups/` - Automatic backups

**To restore:**
1. Extract new portable folder
2. Copy backup files into new folder
3. Run the EXE

---

## 📞 Support

For issues:
1. Check logs in `logs/` folder
2. Verify network connectivity
3. Ensure PLC/OPC UA servers are accessible
4. Try deleting `db.sqlite3` and restarting

---

**Built:** 2026-07-10  
**Version:** v2026.07.10  
**Platform:** Windows 10/11 (64-bit)
