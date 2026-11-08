CREATE TABLE audit_logs (
  id TEXT PRIMARY KEY,
  action TEXT NOT NULL
);

CREATE OR REPLACE FUNCTION record_audit(p_id TEXT) RETURNS VOID AS $$
BEGIN
  INSERT INTO audit_logs (id, action) VALUES (p_id, 'init');
END;
$$ LANGUAGE plpgsql;
