#!/usr/bin/env python3
import sqlite3, json, os, sys
from datetime import datetime

DB = os.environ.get('SQLITE_DB_PATH', '/app/instance/phishclassifier.db')
PERSISTENCE_ID = 136
AUDIT_ID = f'persistence-{PERSISTENCE_ID}'
FILENAMES = {
  'persistence_id': PERSISTENCE_ID,
  'filename': f'{PERSISTENCE_ID}.body.txt',
  'full_filename': f'{PERSISTENCE_ID}.full.body.txt',
  'log_filename': f'{PERSISTENCE_ID}.log'
}

def main():
    if not os.path.exists(DB):
        print('ERROR: sqlite DB not found at', DB, file=sys.stderr)
        return 2
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # try by explicit persistence_id column
    row = None
    try:
        cur.execute('SELECT * FROM task_audit WHERE persistence_id = ? LIMIT 1', (PERSISTENCE_ID,))
        row = cur.fetchone()
    except Exception:
        row = None

    # fallback: scan result_meta text for the number
    if not row:
        try:
            cur.execute('SELECT * FROM task_audit WHERE result_meta IS NOT NULL LIMIT 200')
            candidates = cur.fetchall()
            for c in candidates:
                try:
                    rm = c['result_meta']
                    if not rm:
                        continue
                    if isinstance(rm, (bytes, bytearray)):
                        rm = rm.decode('utf-8', 'replace')
                    if isinstance(rm, str) and str(PERSISTENCE_ID) in rm:
                        row = c
                        break
                except Exception:
                    continue
        except Exception:
            row = None

    now = datetime.utcnow().isoformat()
    if row:
        ta_id = row['id']
        print('Found existing task_audit id=', ta_id)
        existing_rm = row['result_meta'] or ''
        try:
            if isinstance(existing_rm, (bytes, bytearray)):
                existing_rm = existing_rm.decode('utf-8', 'replace')
            parsed = json.loads(existing_rm) if isinstance(existing_rm, str) and existing_rm.strip().startswith('{') else {}
        except Exception:
            parsed = {}
        parsed.update(FILENAMES)
        parsed['resolved_at'] = now
        parsed_text = json.dumps(parsed)
        try:
            cur.execute('UPDATE task_audit SET status = ?, persistence_id = ?, result_meta = ?, updated_at = ? WHERE id = ?',
                        ("succeeded", PERSISTENCE_ID, parsed_text, now, ta_id))
            conn.commit()
            print('Updated task_audit', ta_id)
        except Exception as e:
            print('UPDATE failed:', e, file=sys.stderr)
            conn.rollback()
            conn.close()
            return 3
    else:
        payload_text = json.dumps({'persistence_id': PERSISTENCE_ID})
        result_meta_text = json.dumps(FILENAMES)
        try:
            cur.execute('INSERT INTO task_audit (id, task_id, task_type, "user", payload, status, created_at, updated_at, persistence_id, result_meta) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                        (AUDIT_ID, None, 'classify', None, payload_text, 'succeeded', now, now, PERSISTENCE_ID, result_meta_text))
            conn.commit()
            print('Inserted new task_audit id=', AUDIT_ID)
        except Exception as e:
            print('INSERT failed:', e, file=sys.stderr)
            conn.rollback()
            conn.close()
            return 4

    conn.close()
    return 0

if __name__ == "__main__":
    sys.exit(main())
