#!/usr/bin/env python3
import sqlite3, os, time, shutil, json, sys

DB = "/app/instance/phishclassifier.db"
BAK = DB + ".pre_add_training_job_cols." + str(int(time.time())) + ".bak"
TABLE = "training_job"

def backup():
    shutil.copyfile(DB, BAK)
    print("DB_BACKUP", BAK)

def cols(conn):
    cur = conn.cursor()
    return [c[1] for c in cur.execute(f"PRAGMA table_info('{TABLE}')").fetchall()]

def add_text_col(conn, col):
    cur = conn.cursor()
    try:
        cur.execute(f"ALTER TABLE {TABLE} ADD COLUMN {col} TEXT;")
        conn.commit()
        print("ADDED", col)
    except Exception as e:
        print("ADD_FAILED", col, e)

def add_json_col(conn, col):
    # SQLite has no native JSON type in older versions; keep TEXT and store JSON string
    add_text_col(conn, col)

def backfill(conn):
    cur = conn.cursor()
    # updated_at <- created_at where null or empty
    try:
        cur.execute(f"UPDATE {TABLE} SET updated_at = created_at WHERE updated_at IS NULL OR updated_at = ''")
        # ensure datasets is valid JSON: set to [] if NULL or empty
        cur.execute(f"UPDATE {TABLE} SET datasets = '[]' WHERE datasets IS NULL OR datasets = ''")
        conn.commit()
        print("BACKFILLED")
    except Exception as e:
        print("BACKFILL_ERR", e)

def main():
    if not os.path.exists(DB):
        print("NO_DB", DB); return
    backup()
    conn = sqlite3.connect(DB)
    try:
        existing = cols(conn)
        print("EXISTING", existing)
        # Add columns if missing
        if "task_id" not in existing:
            add_text_col(conn, "task_id")
        if "updated_at" not in existing:
            add_text_col(conn, "updated_at")
        if "started_at" not in existing:
            add_text_col(conn, "started_at")
        if "finished_at" not in existing:
            add_text_col(conn, "finished_at")
        if "datasets" not in existing:
            add_json_col(conn, "datasets")
        backfill(conn)
        print("COLUMNS_NOW", cols(conn))
    finally:
        try:
            conn.close()
        except Exception:
            pass
    # self-delete
    try:
        os.remove(__file__)
        print("SELF_REMOVED", __file__)
    except Exception:
        pass

if __name__ == "__main__":
    main()
