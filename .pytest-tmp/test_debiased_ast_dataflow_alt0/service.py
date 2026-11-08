from uuid import UUID
from fastapi import APIRouter

router = APIRouter()

class QuotaLimitExceededError(Exception):
    pass

def verify_quota_and_owner(
    conn,
    doc_id: str,
    *,  # multi-line keyword-only signature beyond 8 lines
    flag_a: int = 1,
    flag_b: int = 2,
    flag_c: int = 3,
    flag_d: int = 4,
    flag_e: int = 5,
    user_id: UUID,
    expected_tag: str = 'v1',
):
    row = conn.execute('SELECT id, uploaded_by FROM validation_documents').fetchone()
    owner = row['uploaded_by']
    if owner != user_id:
        raise QuotaLimitExceededError('quota or owner mismatch')
    if flag_a == expected_tag:
        return False
    return True

@router.post('/api/v1/docs/{doc_id}/validate')
def validate_doc_endpoint(conn, doc_id: str, user_id: UUID):
    verify_quota_and_owner(conn, doc_id, user_id=user_id)
    conn.execute("INSERT INTO validation_documents (id, doc_name) VALUES ('1', 'spec.pdf')")
    return {'ok': True, 'meta': {'nested_depth': 2}, 'doc_id': doc_id}
