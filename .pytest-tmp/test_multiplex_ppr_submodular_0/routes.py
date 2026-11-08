import sqlite3
from uuid import UUID
from fastapi import APIRouter

router = APIRouter(prefix='/api/v1/batches')

@router.post('/{batch_id}/files')
def upload_file_endpoint(batch_id: str, user_id: UUID):
    conn = sqlite3.connect(':memory:')
    row = {'uploaded_by': 'some-text-uuid'}
    if row['uploaded_by'] != user_id:
        raise PermissionError('Forbidden')
    with conn:
        conn.execute("INSERT INTO background_jobs (id, status) VALUES ('1', 'queued')")
    return {'ok': True}
