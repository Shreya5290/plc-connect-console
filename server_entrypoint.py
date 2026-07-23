"""Click2Connect portable EXE entrypoint.

Purpose:
- Ensure the frozen executable stays alive by explicitly starting Django's server.
- Disable Django's autoreloader (PyInstaller onefile + reloader can crash).
- Keep the server running in the terminal without auto-launching a browser.

Behavior:
- Serves the UI at http://127.0.0.1:8000/ locally and binds the server to
  all interfaces for network access.

Robustness:
- Capture unhandled exceptions into logs/startup_error.log so PyInstaller's
  PYI-3664 hides less information.
"""

import os
import time
import traceback


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


def main() -> int:
    try:
        # Ensure Django settings are discoverable
        os.environ.setdefault("DJANGO_SETTINGS_MODULE", "plc_connect.settings")

        # Let onefile extraction finish
        time.sleep(0.2)

        # URL to display for manual browser access.
        url = "http://127.0.0.1:8000/"
        print(f"[Startup] UI available at {url}")

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

