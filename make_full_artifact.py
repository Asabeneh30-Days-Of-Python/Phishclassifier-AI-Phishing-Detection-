# make_full_artifact.py
import sqlite3, pathlib, json, textwrap, sys
DB = "/app/instance/phishclassifier.db"
pid = 57
p = pathlib.Path(DB)
if not p.exists():
    print("DB not found:", DB); raise SystemExit(1)

con = sqlite3.connect(DB)
cur = con.execute("SELECT sender, received_at, subject, body FROM quarantined_emails WHERE id=?", (pid,))
row = cur.fetchone()
if not row:
    print("no row for", pid); con.close(); raise SystemExit(1)

sender, received_at, subject, body = row
meta = {
    "persistence_id": pid,
    "celery_id": None,
    "created_at": (received_at.isoformat() if hasattr(received_at, "isoformat") else str(received_at)),
    "user": None,
    "source": "classify_email_task (recreated)",
    "task_type": "classify",
    "payload_preview": {"email_id": {"sender": sender}, "subject": subject or None, "content_preview": (body[:120] if body else "")}
}
hdr = "=== PHISHCLASSIFIER BODY ARTIFACT v1\n"
hdr += "metadata: " + json.dumps(meta) + "\n"
hdr += "--- LOGS BEGIN ---\n" + "--- LOGS END ---\n"
hdr += "--- PREDICTION SUMMARY (machine readable) ---\n" + "{}\n"
hdr += "--- RAW BODY START ---\n"

out_full = pathlib.Path(f"/app/instance/reports/{pid}.full.body.txt")
out_raw = pathlib.Path(f"/app/instance/reports/{pid}.body.txt")

out_full.write_text(hdr + (body or "") + "\n--- RAW BODY END ---\n", encoding="utf-8")
out_raw.write_text(body or "", encoding="utf-8")

print("wrote", out_full)
print("wrote", out_raw)
con.close()
