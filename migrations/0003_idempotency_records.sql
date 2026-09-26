-- §7.7 duplicate prevention, plus attempt_id (fencing token, R-005) and semantic_version.
-- The primary key is what enforces exactly-once reservation (§7.6). Bodies never live here:
-- results are stored by reference into the payload store.
CREATE TABLE idempotency_records (
    agent_id          text        NOT NULL,
    tool              text        NOT NULL,
    idem_key          text        NOT NULL CHECK (idem_key ~ '^[A-Za-z0-9._:-]{1,128}$'),
    fingerprint       bytea       NOT NULL CHECK (octet_length(fingerprint) = 32),
    state             text        NOT NULL
                      CHECK (state IN ('RESERVED', 'COMPLETED', 'FAILED_RETRYABLE', 'UNKNOWN')),
    semantic_version  text        NOT NULL,
    attempt_id        uuid        NOT NULL,
    correlation_id    text        NOT NULL,
    result_ref        text,
    error_code        text,
    lease_expires_at  timestamptz CHECK (state <> 'RESERVED' OR lease_expires_at IS NOT NULL),
    expires_at        timestamptz NOT NULL,
    created_at        timestamptz NOT NULL,
    updated_at        timestamptz NOT NULL,
    PRIMARY KEY (agent_id, tool, idem_key)
);

CREATE INDEX idem_open_states ON idempotency_records (state, lease_expires_at)
    WHERE state IN ('RESERVED', 'UNKNOWN');
CREATE INDEX idem_expiry ON idempotency_records (expires_at);
