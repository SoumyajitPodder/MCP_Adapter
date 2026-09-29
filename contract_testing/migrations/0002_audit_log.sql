-- §8.7 tamper-evident audit trail: one hash chain per chain_id (DESIGN.md R-011).
-- The trigger makes the table append-only for ordinary roles; the hash chain is what
-- detects changes made by anyone able to bypass it.
CREATE TABLE audit_log (
    chain_id     text        NOT NULL,
    seq          bigint      NOT NULL CHECK (seq >= 1),
    prev_hash    text        CHECK (prev_hash ~ '^[0-9a-f]{64}$'),
    record_hash  text        NOT NULL CHECK (record_hash ~ '^[0-9a-f]{64}$'),
    recorded_at  timestamptz NOT NULL DEFAULT now(),
    event        jsonb       NOT NULL,
    PRIMARY KEY (chain_id, seq),
    CHECK ((seq = 1) = (prev_hash IS NULL))
);

CREATE FUNCTION audit_log_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'audit_log is append-only';
END;
$$;

CREATE TRIGGER audit_log_no_update_or_delete
    BEFORE UPDATE OR DELETE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION audit_log_append_only();

CREATE TRIGGER audit_log_no_truncate
    BEFORE TRUNCATE ON audit_log
    FOR EACH STATEMENT EXECUTE FUNCTION audit_log_append_only();
