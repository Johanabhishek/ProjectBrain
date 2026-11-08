import sqlite3

def process_next_job():
    conn = sqlite3.connect(':memory:')
    with conn:
        rows = conn.execute("SELECT id, status FROM background_jobs WHERE status = 'queued'").fetchall()
        for r in rows:
            print(r)
    return len(rows)
