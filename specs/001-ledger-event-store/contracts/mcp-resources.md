# Contract: MCP Resources (Query Side)

**Status**: FROZEN after Phase 5 completion (Constitution Principle III)
**Source**: Challenge document Phase 5, "MCP Resources — The Query Side"
**File**: `src/mcp/resources.py`

---

## Design Principles

1. **Projections only, no stream reads**: Resources MUST read from projection
   tables (ApplicationSummary, AgentPerformanceLedger, ComplianceAuditView).
   No `store.load_stream()` calls except the two documented justified exceptions.
2. **SLO-bound**: Each resource has a p99 latency target; the `ledger/health`
   resource must be < 10 ms (watchdog).
3. **Temporal queries**: Two resources support `?as_of=` and `?from=&to=` query
   parameters for regulatory time-travel.

---

## Resource 1: `ledger://applications/{id}`

**Projection source**: `application_summary` table
**Temporal query**: No — current state only
**SLO**: p99 < 50 ms

**Response**:
```json
{
  "application_id": "string",
  "state": "string",
  "applicant_id": "string",
  "requested_amount_usd": "string (decimal)",
  "approved_amount_usd": "string (decimal) | null",
  "risk_tier": "string | null",
  "fraud_score": "string (decimal) | null",
  "compliance_status": "PENDING | PASSED | FAILED",
  "decision": "APPROVE | DECLINE | REFER | null",
  "agent_sessions_completed": ["stream_id"],
  "last_event_type": "string",
  "last_event_at": "ISO datetime",
  "human_reviewer_id": "string | null",
  "final_decision_at": "ISO datetime | null",
  "_meta": {
    "projection_lag_ms": "integer",
    "as_of": "ISO datetime (current time)"
  }
}
```

**Error**: `404 StreamNotFoundError` if `application_id` not in projection.

---

## Resource 2: `ledger://applications/{id}/compliance`

**Projection source**: `compliance_audit_view` table + `compliance_snapshots`
**Temporal query**: Yes — `?as_of=<ISO datetime>`
**SLO**: p99 < 200 ms

**Response** (current state):
```json
{
  "application_id": "string",
  "regulation_set_version": "string",
  "checks_required": ["rule_id"],
  "checks_passed": [
    {
      "rule_id": "string",
      "rule_version": "string",
      "evaluation_timestamp": "ISO datetime",
      "evidence_hash": "string"
    }
  ],
  "checks_failed": [
    {
      "rule_id": "string",
      "rule_version": "string",
      "failure_reason": "string",
      "remediation_required": "boolean"
    }
  ],
  "clearance_granted": "boolean",
  "last_event_at": "ISO datetime",
  "_meta": {
    "projection_lag_ms": "integer",
    "as_of": "ISO datetime (now or ?as_of value)",
    "snapshot_used": "boolean"
  }
}
```

**Temporal query**: With `?as_of=2026-01-15T10:00:00Z`, returns compliance state
as it existed at that timestamp. Uses `compliance_snapshots` + event replay from
nearest preceding snapshot.

---

## Resource 3: `ledger://applications/{id}/audit-trail`

**Projection source**: `AuditLedger` stream (direct load — justified exception)
**Temporal query**: Yes — `?from=<ISO>&to=<ISO>` range filter
**SLO**: p99 < 500 ms
**Justified exception**: AuditLedger direct load is explicitly documented in the
challenge spec as a justified exception. The audit trail's value IS the
chronological event stream; no projection adds value here.

**Response**:
```json
{
  "application_id": "string",
  "events": [
    {
      "event_id": "UUID",
      "event_type": "string",
      "event_version": "integer",
      "stream_id": "string",
      "stream_position": "integer",
      "global_position": "integer",
      "recorded_at": "ISO datetime",
      "payload": {},
      "metadata": {
        "correlation_id": "string | null",
        "causation_id": "string | null"
      }
    }
  ],
  "integrity_status": {
    "last_check_at": "ISO datetime | null",
    "chain_valid": "boolean | null",
    "events_since_last_check": "integer"
  },
  "_meta": {
    "total_events": "integer",
    "from": "ISO datetime | null",
    "to": "ISO datetime | null"
  }
}
```

---

## Resource 4: `ledger://agents/{id}/performance`

**Projection source**: `agent_performance_ledger` table
**Temporal query**: No — current metrics only
**SLO**: p99 < 50 ms

**Response**:
```json
{
  "agent_id": "string",
  "model_versions": [
    {
      "model_version": "string",
      "analyses_completed": "integer",
      "decisions_generated": "integer",
      "avg_confidence_score": "float | null",
      "avg_duration_ms": "float | null",
      "approve_rate": "float | null",
      "decline_rate": "float | null",
      "refer_rate": "float | null",
      "human_override_rate": "float | null",
      "first_seen_at": "ISO datetime",
      "last_seen_at": "ISO datetime"
    }
  ]
}
```

---

## Resource 5: `ledger://agents/{id}/sessions/{session_id}`

**Projection source**: `AgentSession` stream (direct load — justified exception)
**Temporal query**: Yes — full replay capability
**SLO**: p99 < 300 ms
**Justified exception**: Full agent session replay is the Gas Town recovery
mechanism. The value is the complete ordered event history, not a summarised view.

**Response**:
```json
{
  "agent_id": "string",
  "session_id": "string",
  "events": [
    {
      "event_id": "UUID",
      "event_type": "string",
      "stream_position": "integer",
      "recorded_at": "ISO datetime",
      "payload": {}
    }
  ],
  "health_status": "ACTIVE | NEEDS_RECONCILIATION | COMPLETED",
  "context_loaded": "boolean",
  "model_version": "string | null"
}
```

---

## Resource 6: `ledger://ledger/health`

**Projection source**: `ProjectionDaemon.get_all_lags()`
**Temporal query**: No — real-time only
**SLO**: p99 < 10 ms (watchdog endpoint — must be fastest in the system)
**No database queries**: Returns cached in-memory lag values from the daemon.

**Response**:
```json
{
  "status": "healthy | degraded | critical",
  "projections": {
    "application_summary": {
      "lag_ms": "integer",
      "slo_ms": 500,
      "slo_breached": "boolean",
      "last_position": "integer",
      "last_updated_at": "ISO datetime"
    },
    "agent_performance_ledger": {
      "lag_ms": "integer",
      "slo_ms": 500,
      "slo_breached": "boolean",
      "last_position": "integer",
      "last_updated_at": "ISO datetime"
    },
    "compliance_audit_view": {
      "lag_ms": "integer",
      "slo_ms": 2000,
      "slo_breached": "boolean",
      "last_position": "integer",
      "last_updated_at": "ISO datetime"
    }
  },
  "event_store": {
    "latest_global_position": "integer",
    "total_streams": "integer"
  },
  "_meta": {
    "checked_at": "ISO datetime"
  }
}
```

**Status logic**:
- `healthy`: no SLO breaches
- `degraded`: one or more projections exceed SLO by < 3×
- `critical`: one or more projections exceed SLO by ≥ 3× for ≥ 5 consecutive cycles
