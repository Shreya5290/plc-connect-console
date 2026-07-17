import sqlite3, os

DB='db.sqlite3'
print('cwd:', os.getcwd())
print('db exists:', os.path.exists(DB))

if not os.path.exists(DB):
    raise SystemExit(0)

con=sqlite3.connect(DB)
cur=con.cursor()
cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='django_session'")
print('django_session table:', cur.fetchone())
con.close()

