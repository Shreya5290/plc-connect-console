"""Click2Connect portable EXE entrypoint.

Purpose:
- Ensure the frozen executable stays alive by explicitly starting Django's server.
- Disable Django's autoreloader (PyInstaller onefile + reloader can crash).
- Open the browser automatically on startup.

Behavior:
- Uses the same UI URL as the server bind address: http://127.0.0.1:8000
  (On remote PCs, this will open the local browser of that PC.)
"""

import os
import time
import webbrowser


def main() -> int:
    # Ensure Django settings are discoverable
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "plc_connect.settings")

    # Let onefile extraction finish
    time.sleep(0.2)

    # URL to display/open (local host)
    url = "http://127.0.0.1:8000/"

    # Open browser shortly after server starts
    try:
        webbrowser.open(url)
    except Exception:
        # Non-fatal on headless PCs
        pass

    # Ensure DB/migrations (including django_session) exist before serving.
    # Fail-fast if the sessions table is missing; otherwise first request will 500.
    from django.conf import settings as django_settings
    from django.core.management import call_command
    import sqlite3

    print(f"[Startup] resolved BASE_DIR={getattr(django_settings, 'BASE_DIR', None)}")

    # Run all migrations first.
    call_command(
        "migrate",
        verbosity=0,
        interactive=False,
        run_syncdb=True,
        database="default",
    )

    # Extra force for sessions app.
    call_command(
        "migrate",
        "sessions",
        verbosity=0,
        interactive=False,
        run_syncdb=True,
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
            print("[Startup] django_session missing; running migrate again for sessions")
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


if __name__ == "__main__":
    raise SystemExit(main())

