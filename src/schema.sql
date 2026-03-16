-- The Ledger — PostgreSQL Event Store Schema
-- Challenge-specified tables are reproduced exactly.
-- Justified additions: compliance_snapshots, projection_errors
-- (documented in DESIGN.md §Projection Strategy)

-- ---------------------------------------------------------------------------
-- Core event log
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS events (
    event_id        UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    stream_id       TEXT        NOT NULL,
    stream_position BIGINT      NOT NULL,
    global_position BIGINT      GENERATED ALWAYS AS IDENTITY,
    event_type      TEXT        NOT NULL,
    event_version   SMALLINT    NOT NULL DEFAULT 1,
    payload         JSONB       NOT NULL,
    metadata        JSONB       NOT NULL DEFAULT '{}'::jsonb,
    recorded_at     TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT uq_stream_position UNIQUE (stream_id, stream_position)
);

CREATE INDEX IF NOT EXISTS idx_events_stream_id    ON events (stream_id, stream_position);
CREATE INDEX IF NOT EXISTS idx_events_global_pos   ON events (global_position);
CREATE INDEX IF NOT EXISTS idx_events_type         ON events (event_type);
CREATE INDEX IF NOT EXISTS idx_events_recorded     ON events (recorded_at);

-- ---------------------------------------------------------------------------
-- Stream registry — one row per aggregate stream
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS event_streams (
    stream_id      TEXT        PRIMARY KEY,
    aggregate_type TEXT        NOT NULL,
    current_version BIGINT     NOT NULL DEFAULT 0,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    archived_at    TIMESTAMPTZ,
    metadata       JSONB       NOT NULL DEFAULT '{}'::jsonb
);

-- ---------------------------------------------------------------------------
-- Projection checkpoints — daemon stores last processed global_position here
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS projection_checkpoints (
    projection_name TEXT   PRIMARY KEY,
    last_position   BIGINT NOT NULL DEFAULT 0,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ---------------------------------------------------------------------------
-- Outbox — guaranteed event delivery (internal; Week 10 connects to Kafka)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS outbox (
    id           UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    event_id     UUID        NOT NULL REFERENCES events(event_id),
    destination  TEXT        NOT NULL,
    payload      JSONB       NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    published_at TIMESTAMPTZ,
    attempts     SMALLINT    NOT NULL DEFAULT 0
);

-- ---------------------------------------------------------------------------
-- Compliance snapshots — temporal query support for ComplianceAuditView
-- Triggered every 100 compliance events per application_id.
-- Justified addition: required to meet get_compliance_at() p99 < 200ms SLO.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS compliance_snapshots (
    snapshot_id      UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    application_id   TEXT        NOT NULL,
    snapshot_at      TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    event_position   BIGINT      NOT NULL,
    snapshot_payload JSONB       NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_compliance_snapshots_app
    ON compliance_snapshots (application_id, snapshot_at DESC);

-- ---------------------------------------------------------------------------
-- Projection errors — fault-tolerant daemon skip log
-- Justified addition: enables root-cause analysis for skipped events.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS projection_errors (
    error_id        UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    projection_name TEXT        NOT NULL,
    global_position BIGINT      NOT NULL,
    event_type      TEXT        NOT NULL,
    error_message   TEXT        NOT NULL,
    error_at        TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    retry_count     SMALLINT    NOT NULL DEFAULT 0
);

-- ---------------------------------------------------------------------------
-- ApplicationSummary projection table
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS application_summary (
    application_id           TEXT        PRIMARY KEY,
    state                    TEXT        NOT NULL,
    applicant_id             TEXT,
    requested_amount_usd     NUMERIC,
    approved_amount_usd      NUMERIC,
    risk_tier                TEXT,
    fraud_score              NUMERIC,
    compliance_status        TEXT,
    decision                 TEXT,
    agent_sessions_completed JSONB       NOT NULL DEFAULT '[]'::jsonb,
    last_event_type          TEXT,
    last_event_at            TIMESTAMPTZ,
    human_reviewer_id        TEXT,
    final_decision_at        TIMESTAMPTZ,
    updated_at               TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ---------------------------------------------------------------------------
-- AgentPerformanceLedger projection table
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS agent_performance_ledger (
    agent_id              TEXT    NOT NULL,
    model_version         TEXT    NOT NULL,
    analyses_completed    INTEGER NOT NULL DEFAULT 0,
    decisions_generated   INTEGER NOT NULL DEFAULT 0,
    avg_confidence_score  NUMERIC,
    avg_duration_ms       NUMERIC,
    approve_rate          NUMERIC,
    decline_rate          NUMERIC,
    refer_rate            NUMERIC,
    human_override_rate   NUMERIC,
    first_seen_at         TIMESTAMPTZ,
    last_seen_at          TIMESTAMPTZ,
    PRIMARY KEY (agent_id, model_version)
);

-- ---------------------------------------------------------------------------
-- ComplianceAuditView projection table
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS compliance_audit_view (
    application_id         TEXT        NOT NULL,
    rule_id                TEXT        NOT NULL,
    rule_version           TEXT        NOT NULL,
    regulation_set_version TEXT,
    status                 TEXT        NOT NULL,  -- PASSED | FAILED | PENDING
    failure_reason         TEXT,
    remediation_required   BOOLEAN,
    evidence_hash          TEXT,
    evaluation_timestamp   TIMESTAMPTZ,
    recorded_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (application_id, rule_id)
);
