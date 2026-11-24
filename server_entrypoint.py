"""Click2Connect portable EXE entrypoint.

Purpose:
- Ensure the frozen executable stays alive by explicitly starting Django's server.
- Disable Django's autoreloader (PyInstaller onefile + reloader can crash).
- Run from the Windows notification area to prevent accidental closure.

Behavior:
- Serves the UI at http://127.0.0.1:8000/ locally and binds the server to
  all interfaces for network access.
- Opens the browser on startup. The tray menu can reopen the UI or explicitly
  stop Click2Connect.

Robustness:
- Capture unhandled exceptions into logs/startup_error.log so PyInstaller's
  PYI-3664 hides less information.
"""

import logging
import os
import socket
import sys
import threading
import time
import traceback
import webbrowser
from logging.handlers import RotatingFileHandler
from pathlib import Path


APP_URL = "http://127.0.0.1:8000/"


class _LogStream:
    def __init__(self, logger: logging.Logger, level: int, target_stream=None) -> None:
        self._logger = logger
        self._level = level
        self._target_stream = target_stream
        self._buffer = ""
        self._lock = threading.Lock()

    def write(self, message: str) -> int:
        message = str(message)
        if self._target_stream is not None:
            self._target_stream.write(message)
        with self._lock:
            self._buffer += message
            while "\n" in self._buffer:
                line, self._buffer = self._buffer.split("\n", 1)
                if line.strip():
                    self._logger.log(self._level, line.rstrip("\r"))
        return len(message)

    def flush(self) -> None:
        if self._target_stream is not None:
            self._target_stream.flush()
        with self._lock:
            if self._buffer.strip():
                self._logger.log(self._level, self._buffer.rstrip("\r"))
            self._buffer = ""

    def isatty(self) -> bool:
        return bool(
            self._target_stream is not None
            and getattr(self._target_stream, "isatty", lambda: False)()
        )


def _configure_logging() -> None:
    app_dir = (
        Path(sys.executable).resolve().parent
        if getattr(sys, "frozen", False)
        else Path(__file__).resolve().parent
    )
    logs_dir = app_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    handler = RotatingFileHandler(
        logs_dir / "click2connect.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter(
        "[%(levelname)s] %(asctime)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
    sys.stdout = _LogStream(logging.getLogger("stdout"), logging.INFO, sys.stdout)
    sys.stderr = _LogStream(logging.getLogger("stderr"), logging.ERROR, sys.stderr)


def _disable_console_close_button() -> None:
    if os.name != "nt":
        return
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetConsoleWindow.restype = wintypes.HWND
    kernel32.GetConsoleProcessList.argtypes = [
        ctypes.POINTER(wintypes.DWORD),
        wintypes.DWORD,
    ]
    kernel32.GetConsoleProcessList.restype = wintypes.DWORD
    process_ids = (wintypes.DWORD * 64)()
    attached_processes = kernel32.GetConsoleProcessList(process_ids, len(process_ids))
    if attached_processes != 1:
        return
    console_window = kernel32.GetConsoleWindow()
    if not console_window:
        return

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetSystemMenu.argtypes = [wintypes.HWND, wintypes.BOOL]
    user32.GetSystemMenu.restype = wintypes.HMENU
    user32.EnableMenuItem.argtypes = [wintypes.HMENU, wintypes.UINT, wintypes.UINT]
    user32.EnableMenuItem.restype = wintypes.UINT
    user32.GetMenuItemCount.argtypes = [wintypes.HMENU]
    user32.GetMenuItemCount.restype = ctypes.c_int
    user32.GetMenuItemID.argtypes = [wintypes.HMENU, ctypes.c_int]
    user32.GetMenuItemID.restype = wintypes.UINT
    user32.DeleteMenu.argtypes = [wintypes.HMENU, wintypes.UINT, wintypes.UINT]
    user32.DeleteMenu.restype = wintypes.BOOL
    user32.DrawMenuBar.argtypes = [wintypes.HWND]
    user32.DrawMenuBar.restype = wintypes.BOOL
    system_menu = user32.GetSystemMenu(console_window, False)
    if not system_menu:
        return

    sc_close = 0xF060
    sc_minimize = 0xF020
    sc_maximize = 0xF030
    mf_byposition = 0x00000400
    mf_bycommand = 0x00000000
    mf_grayed = 0x00000001
    item_count = user32.GetMenuItemCount(system_menu)
    if item_count < 0:
        raise ctypes.WinError(ctypes.get_last_error())
    for position in range(item_count - 1, -1, -1):
        command = user32.GetMenuItemID(system_menu, position)
        if command not in (sc_minimize, sc_maximize, sc_close):
            if not user32.DeleteMenu(system_menu, position, mf_byposition):
                raise ctypes.WinError(ctypes.get_last_error())
    if user32.EnableMenuItem(
        system_menu, sc_close, mf_bycommand | mf_grayed
    ) == 0xFFFFFFFF:
        raise ctypes.WinError(ctypes.get_last_error())
    if not user32.DrawMenuBar(console_window):
        raise ctypes.WinError(ctypes.get_last_error())


def _log_error(msg: str) -> None:
    """Portable-safe logging to logs/startup_error.log (inside app runtime dir)."""
    try:
        logs_dir = os.path.join(os.getcwd(), "logs")
        os.makedirs(logs_dir, exist_ok=True)
        log_path = os.path.join(logs_dir, "startup_error.log")
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(msg)
            if not msg.endswith("\n"):
                f.write("\n")
    except Exception:
        # Last resort: do nothing
        pass


def _open_browser_when_ready() -> None:
    """Open the UI only after the local server accepts connections."""
    for _ in range(100):
        try:
            with socket.create_connection(("127.0.0.1", 8000), timeout=0.2):
                webbrowser.open(APP_URL)
                return
        except OSError:
            time.sleep(0.1)


def _start_tray_icon() -> None:
    """Start the notification-area icon without blocking Django's main thread."""
    import pystray
    from PIL import Image, ImageDraw

    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse((4, 4, 60, 60), fill=(0, 120, 212, 255))
    draw.text((21, 14), "C", fill="white", stroke_width=1)

    def open_ui(icon: pystray.Icon, item: pystray.MenuItem) -> None:
        del icon, item
        webbrowser.open(APP_URL)

    def exit_app(icon: pystray.Icon, item: pystray.MenuItem) -> None:
        del item
        icon.stop()
        os._exit(0)

    menu = pystray.Menu(
        pystray.MenuItem("Open Click2Connect", open_ui, default=True),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Exit Click2Connect", exit_app),
    )
    icon = pystray.Icon("Click2Connect", image, "Click2Connect is running", menu)
    icon.run_detached()


def main() -> int:
    try:
        _configure_logging()
        _disable_console_close_button()

        # Ensure Django settings are discoverable
        os.environ.setdefault("DJANGO_SETTINGS_MODULE", "plc_connect.settings")

        # Hide Django's "development server / not for production" banner. This is
        # a deliberate single-user local tool, so the warning is noise.
        os.environ.setdefault("DJANGO_RUNSERVER_HIDE_WARNING", "true")

        # Let onefile extraction finish
        time.sleep(0.2)

        print(f"[Startup] UI available at {APP_URL}")

        # Ensure DB/migrations (including django_session) exist before serving.
        # IMPORTANT: call_command requires Django apps registry to be ready.
        from django.conf import settings as django_settings
        from django.core.management import call_command
        import django
        import sqlite3

        # Initialize Django app registry BEFORE migrations.
        django.setup()

        print(
            f"[Startup] resolved BASE_DIR={getattr(django_settings, 'BASE_DIR', None)}"
        )

        # Run all migrations first.
        call_command(
            "migrate",
            verbosity=0,
            interactive=False,
            run_syncdb=True,
            database="default",
        )

        # Extra force for sessions app.
        # Django 6 rejects `run_syncdb=True` for apps that already have migrations.
        # So we run without run_syncdb for sessions.
        call_command(
            "migrate",
            "sessions",
            verbosity=0,
            interactive=False,
            database="default",
        )


        # Verify django_session table exists for SQLite only.
        db_default = django_settings.DATABASES.get("default", {})
        db_engine = db_default.get("ENGINE")
        db_name = db_default.get("NAME")

        if db_engine == "django.db.backends.sqlite3" and db_name:
            con = sqlite3.connect(str(db_name))
            cur = con.cursor()
            cur.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='django_session'"
            )
            exists = cur.fetchone() is not None
            con.close()

            if not exists:
                # One more attempt after verifying state.
                print(
                    "[Startup] django_session missing; running migrate again for sessions"
                )
                call_command(
                    "migrate",
                    "sessions",
                    verbosity=0,
                    interactive=False,
                    run_syncdb=True,
                    database="default",
                )

                con = sqlite3.connect(str(db_name))
                cur = con.cursor()
                cur.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='django_session'"
                )
                exists = cur.fetchone() is not None
                con.close()

                if not exists:
                    raise RuntimeError(
                        "[Startup] django_session table is still missing after migrations. "
                        f"DB={db_name}"
                    )

        from django.core.management import execute_from_command_line

        _start_tray_icon()
        threading.Thread(
            target=_open_browser_when_ready,
            name="browser-launcher",
            daemon=True,
        ).start()

        # Bind to all interfaces for connectivity, but still present local URL
        args = [
            "manage.py",
            "runserver",
            "0.0.0.0:8000",
            "--noreload",
        ]

        execute_from_command_line(args)
        return 0

    except Exception as e:
        tb = traceback.format_exc()
        _log_error(
            "[Startup] UNHANDLED EXCEPTION\n"
            f"{type(e).__name__}: {e}\n\n"
            f"Traceback:\n{tb}\n\n"
        )
        # Re-raise so PyInstaller prints too, but we also have the log.
        raise


if __name__ == "__main__":
    raise SystemExit(main())

