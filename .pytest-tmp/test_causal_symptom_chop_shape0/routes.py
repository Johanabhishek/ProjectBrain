import sqlite3
from uuid import UUID
from fastapi import APIRouter
from deps import load_batch

router = APIRouter(prefix='/api/v1/batches')

@router.post('/{batch_id}/files')
def upload_file_endpoint(batch_id: UUID, user_id: UUID):
    conn = sqlite3.connect(':memory:')
    def on_file_headers():
        return load_batch(conn, batch_id, user_id)
    on_file_headers()
    with conn:
        conn.execute("INSERT INTO background_jobs (id, status) VALUES ('1', 'queued')")
    return {'ok': True, 'file_id': '123'}
