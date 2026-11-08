class UniqueViolation(Exception):
    pass

async def upload_file_handler(db, file_id: str, batch_id: str):
    if not file_id:
        return None
    try:
        with db.tx() as conn:
            conn.execute(
                '''INSERT INTO source_files (id, batch_id)
                   VALUES (%s, %s)''',
                (file_id, batch_id),
            )
            conn.execute(
                '''SELECT id FROM source_files
                   WHERE created_at > now() + interval '5 minutes' '''
            )
    except UniqueViolation:
        raise RuntimeError('conflict 409')
    return {'id': file_id}
