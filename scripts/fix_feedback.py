#!/usr/bin/env python3
import sqlite3
import os
import shutil
from api.feedback_utils import ALLOWED_FEEDBACK

DB_PATH = os.environ.get("PHISH_DB_PATH", "/app/instance/phishclassifier.db")

def backup_db(path):
    bak = path + ".bak"
    if os.path.exists(bak):
        print("Backup already exists:", bak)
    else:
        shutil.copy2(path, bak)
        print("Backup created:", bak)

def normalize_db(path):
    con = sqlite3.connect(path)
    cur = con.cursor()
    cur.execute("UPDATE quarantined_emails SET feedback = 'none' WHERE feedback IS NULL OR feedback = ''")
    placeholders = ",".join("?" for _ in ALLOWED_FEEDBACK)
    cur.execute(
        "UPDATE quarantined_emails SET feedback = 'none' WHERE feedback NOT IN ({})".format(placeholders),
        tuple(ALLOWED_FEEDBACK)
    )
    con.commit()
    cur.execute("SELECT feedback, COUNT(*) FROM quarantined_emails GROUP BY feedback")
    counts = cur.fetchall()
    print("Post-normalization counts:", counts)
    con.close()

if __name__ == "__main__":
    if not os.path.exists(DB_PATH):
        raise SystemExit(f"DB not found at {DB_PATH}")
    backup_db(DB_PATH)
    normalize_db(DB_PATH)
    print("Normalization complete.")
