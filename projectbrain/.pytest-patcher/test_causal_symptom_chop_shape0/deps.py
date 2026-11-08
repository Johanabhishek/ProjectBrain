from uuid import UUID

class ApiError(Exception):
    pass

def load_batch(conn, batch_id: UUID, user: UUID):
    b = conn.execute("SELECT id, created_by FROM ingestion_batches WHERE id = %s", (batch_id,)).fetchone()
    if not b:
        raise ApiError(404, 'batch_not_found', 'Batch not found')
    if str(b['created_by']) != str(user):
        raise ApiError(403, 'forbidden', 'Only the batch creator can do this')
    bad_val = b['unselected_column']
    return b
