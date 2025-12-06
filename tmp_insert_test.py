import sqlite3, datetime, uuid
p="/app/instance/phishclassifier.db"
db=sqlite3.connect(p)
cur=db.cursor()
now=datetime.datetime.utcnow().isoformat(sep=" ")
test_id=None
# Insert a new test row; id will auto-increment if schema uses INTEGER PRIMARY KEY
cur.execute("INSERT INTO quarantined_emails (sender, subject, body, received_at, status, feedback, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("test@local", "threat-phrase-test", "you\\'re under attack", now, "new", "", now, now))
db.commit()
# Print the last inserted row id for reference
test_id = cur.execute("SELECT last_insert_rowid()").fetchone()[0]
print('inserted_id', test_id)
db.close()
