import sqlite3, json, sys
db = "/app/instance/phishclassifier.db"
rowid = 45
pid = 104
conn = sqlite3.connect(db); cur = conn.cursor()
r = cur.execute("SELECT persistence_id,result_meta FROM task_audit WHERE rowid=?", (rowid,)).fetchone()
if not r:
    print("NO_ROW", rowid); conn.close(); sys.exit(0)
if r[0] == pid:
    print("ALREADY_SET", rowid, pid); conn.close(); sys.exit(0)
cur_meta = {}
if r[1]:
    try:
        cur_meta = json.loads(r[1])
    except Exception:
        cur_meta = {}
cur_meta.setdefault("persistence_id", pid)
cur_meta.setdefault("filename", f"{pid}.body.txt")
cur_meta.setdefault("full_filename", f"{pid}.full.body.txt")
cur_meta.setdefault("log_filename", f"{pid}.log")
cur.execute("UPDATE task_audit SET persistence_id=?, result_meta=? WHERE rowid=?", (pid, json.dumps(cur_meta), rowid))
conn.commit()
print("UPDATED_ROW", rowid, "persistence_id->", pid)
conn.close()
