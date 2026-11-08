CREATE TABLE validation_documents (
    id TEXT PRIMARY KEY,
    doc_name TEXT NOT NULL,
    validator_id TEXT NOT NULL,
    approver_id TEXT NOT NULL
);
