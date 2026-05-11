import sqlite3, base64, sys, os

db_path = '/app/output/cryptoverdad.db'

if not os.path.exists(db_path):
    print(f"ERROR: {db_path} no encontrado", file=sys.stderr)
    sys.exit(1)

conn = sqlite3.connect(db_path)
cursor = conn.cursor()
cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
tablas = [r[0] for r in cursor.fetchall()]
print(f"TABLAS: {tablas}", file=sys.stderr)
for t in tablas:
    try:
        cursor.execute(f'SELECT COUNT(*) FROM "{t}"')
        print(f"  {t}: {cursor.fetchone()[0]} registros", file=sys.stderr)
    except Exception as e:
        print(f"  {t}: ERROR {e}", file=sys.stderr)
conn.close()

size = os.path.getsize(db_path)
print(f"Tamaño DB: {size/1024:.1f} KB", file=sys.stderr)

with open(db_path, 'rb') as f:
    data = f.read()
print(base64.b64encode(data).decode())
