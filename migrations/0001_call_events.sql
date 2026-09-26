-- §8.3 call events that were kept (never-sampled events plus sampled reads).
-- Queried by correlation ID for `adapter-verify trace` (§8.9). Bodies never live here:
-- only payload_ref inside the event.
CREATE TABLE call_events (
    id              bigserial   PRIMARY KEY,
    correlation_id  text        NOT NULL,
    occurred_at     timestamptz NOT NULL,
    schema_version  integer     NOT NULL,
    event           jsonb       NOT NULL
);

CREATE INDEX call_events_by_correlation ON call_events (correlation_id, occurred_at, id);
CREATE INDEX call_events_by_time ON call_events (occurred_at);
