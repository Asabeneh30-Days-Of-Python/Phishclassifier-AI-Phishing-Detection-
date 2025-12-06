#!/usr/bin/env python3
import sqlite3, os, time, shutil, sys

DB = "/app/instance/phishclassifier.db"
BAK = DB + ".pre_add_updated_at." + str(int(time.time())) + ".bak"
TABLE = "training_job"
COL = "updated_at"

def backup():
    try:
        shutil.copyfile(DB, BAK)
        print("DB_BACKUP", BAK)
    except Exception as e:
        print("BACKUP_FAILED", e); sys.exit(1)

def has_column(conn, col):
    cur = conn.cursor()
    cols = [c[1] for c in cur.execute(f"PRAGMA table_info('{TABLE}')").fetchall()]
    return col in cols

def add_column(conn, col):
    cur = conn.cursor()
    try:
        cur.execute(f"ALTER TABLE {TABLE} ADD COLUMN {col} TEXT;")
        conn.commit()
        print("ALTERED", f"added column {col} to {TABLE}")
    except Exception as e:
        print("ALTER_FAILED", e); sys.exit(1)

def backfill(conn, col):
    cur = conn.cursor()
    # set updated_at to created_at for rows where updated_at is NULL or empty
    try:
        cur.execute(f"UPDATE {TABLE} SET {col}=created_at WHERE {col} IS NULL OR {col}='';")
        conn.commit()
        print("BACKFILLED", cur.rowcount, "rows")
    except Exception as e:
        print("BACKFILL_FAILED", e)

def verify(conn):
    cur = conn.cursor()
    cols = [c[1] for c in cur.execute(f"PRAGMA table_info('{TABLE}')").fetchall()]
    print("COLUMNS_NOW", cols)
    print("ROWCOUNT", conn.execute(f"SELECT count(1) FROM {TABLE}").fetchone())

def main():
    if not os.path.exists(DB):
        print("NO_DB", DB); return
    backup()
    conn = sqlite3.connect(DB)
    try:
        if has_column(conn, COL):
            print("SKIPPED: column already exists", COL)
        else:
            add_column(conn, COL)
            backfill(conn, COL)
        verify(conn)
    finally:
        try:
            conn.close()
        except Exception:
            pass
    # remove this file inside the container if present
    try:
        os.remove(__file__)
        print("SELF_REMOVED", __file__)
    except Exception:
        pass

if __name__ == "__main__":
    main()
