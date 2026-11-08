import sqlite3

def init_schema(conn):
    conn.executescript('''
    CREATE TABLE IF NOT EXISTS source_files (
        id TEXT PRIMARY KEY,
        batch_id TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    ''')

class SqliteAdapter:
    def _translate_sql(self, sql: str) -> str:
        return sql.replace('%s', '?')
    def execute(self, sql: str, params=()):
        conn = sqlite3.connect(':memory:')
        return conn.execute(self._translate_sql(sql), params)
