# Feature Specification: The Ledger — Agentic Event Store & Enterprise Audit Infrastructure

**Feature Branch**: `001-ledger-event-store`
**Created**: 2026-03-16
**Status**: Draft  
**Challenge**: TRP1 Week 5

---

## Clarifications

### Session 2026-03-16

- Q: Does the MCP server validate the identity and role of callers, or does it trust the identity supplied in the request payload? → A: Caller-supplied identity, no server-side enforcement — `reviewer_id` and role are request parameters recorded in the event payload for audit; access control is a caller/perimeter responsibility.

---

## User Scenarios & Testing *(mandatory)*

### User Story 1 — Regulatory Examiner: Complete Decision History in Under 60 Seconds (Priority: P1)

A regulatory examiner at Apex Financial Services has received a query from
a government auditor asking for the complete decision history of commercial loan
application ID X. The examiner needs to produce, on demand, a full chronological
record: every AI agent action, every compliance check, every human review decision,
all causal links between actions, and cryptographic proof that the record has not
been tampered with. The entire retrieval must complete within 60 seconds.

**Why this priority**: This is the Week Standard — the minimum passing demonstration
for the challenge. It directly addresses the regulatory environment mandate that
Apex's CTO specified: auditability must be the architecture, not an annotation.
An examiner who cannot run this query is an examiner with no audit capability.

**Independent Test**: Can be fully tested by submitting a complete loan application
lifecycle (submission → AI analysis → compliance → decision → human review →
approval/decline), then querying the full event history for that application and
measuring elapsed time. Delivers a complete, verifiable audit trail as its output.

**Acceptance Scenarios**:

1. **Given** a loan application has completed the full lifecycle (all AI agent
   analyses, compliance checks, human review, and final decision recorded),
   **When** a query for the complete decision history of that application is made,
   **Then** the system returns every event in chronological order, each event
   carrying its full payload, all causal links (which prior event triggered each
   subsequent event), the cryptographic integrity verification result, and the
   query completes in under 60 seconds from initiation to full result.

2. **Given** a loan application has events recorded by multiple AI agents
   simultaneously (concurrency scenario),
   **When** the decision history is queried,
   **Then** the history reflects a consistent, non-contradictory sequence — no
   duplicate decisions, no split-brain state — proving that exactly one agent's
   concurrent decision won and the other was correctly rejected.

3. **Given** the audit trail has been tampered with (a stored event payload
   was modified directly in the database),
   **When** the cryptographic integrity check is run,
   **Then** the system reports tamper detection as true, identifies the point in
   the chain where the hash mismatch occurs, and returns a chain_valid = false result.

---

### User Story 2 — AI Agent: Record Decisions with Context Continuity After Crash (Priority: P2)

An AI agent (Credit Analysis, Fraud Detection, Compliance, or Orchestrator) needs
to record its decisions and actions to the Ledger as they happen. If the agent
process crashes mid-session, it must be able to restart and reconstruct its exact
working context from the Ledger alone — without any human intervention — and
continue where it left off without repeating already-completed work.

**Why this priority**: This is the Gas Town pattern — the core architectural
guarantee that prevents ephemeral memory failure in multi-agent systems. Without it,
an agent crash loses all in-progress work and corrupts the loan application state.
With it, agents are restartable, auditable, and their decisions are permanently
recorded before execution.

**Independent Test**: Can be fully tested by starting an agent session, recording
several events, simulating a crash (discarding the in-memory agent object), calling
context reconstruction, and verifying the reconstructed context contains sufficient
information for the agent to continue correctly, including a clear flag on any
partial (unresolved) decision state.

**Acceptance Scenarios**:

1. **Given** an AI agent has started a session and recorded 5 or more events
   (including a partial decision with no corresponding completion event),
   **When** the agent process is restarted and requests context reconstruction
   from the Ledger,
   **Then** the system returns a context object containing: the last completed
   action, all pending work items, a concise summary of prior events (within a
   token budget), the verbatim last 3 events, and a NEEDS_RECONCILIATION flag
   indicating that a partial decision must be resolved before new work proceeds.

2. **Given** an AI agent attempts to record a decision event without first
   establishing a context-loaded declaration for its session,
   **When** the decision recording is attempted,
   **Then** the system rejects the operation with a typed error identifying the
   missing precondition, and the error includes a suggested action pointing to
   the context-loading step.

3. **Given** two AI agents simultaneously attempt to record analysis results
   for the same loan application at the same version of the application stream,
   **When** both operations are submitted concurrently,
   **Then** exactly one succeeds and its event is recorded; the other receives a
   typed concurrency error containing the expected version, the actual version,
   and a suggested action of reload-and-retry; the total event count on the stream
   reflects exactly one new event (not two).

---

### User Story 3 — Loan Officer: Submit Application and Record Human Review Decision (Priority: P3)

A human loan officer needs to submit a new commercial loan application to the
system and later record their final human review decision (approve, decline, or
override the AI recommendation) after reviewing the AI agents' analysis and
recommendations. Both actions must be permanently and immutably recorded in the Ledger.

**Why this priority**: Loan officers are the human-in-the-loop for regulated
decisions. Their submissions and review decisions are legally required events in
the Apex audit trail. Without human review recording, no final decision event can
be appended, and the application lifecycle cannot close.

**Independent Test**: Can be fully tested by submitting a new loan application,
verifying the stream is created with the submitted event, triggering all AI
analysis steps, then recording a human review decision (with override) and
verifying the final state reflects the officer's decision, including the
override reason if provided.

**Acceptance Scenarios**:

1. **Given** a loan officer submits a new commercial loan application with a
   valid applicant ID, requested amount, and loan purpose,
   **When** the submission is recorded,
   **Then** a new application stream is created with an ApplicationSubmitted
   event as its first event, the stream ID follows the `loan-{application_id}`
   format, and the initial stream version is returned to the caller.

2. **Given** an AI orchestrator has generated a decision with recommendation
   APPROVE and confidence_score >= 0.6, and all required compliance checks
   have passed,
   **When** a loan officer records their human review with override = true
   and provides an override reason,
   **Then** a HumanReviewCompleted event is appended referencing the reviewer's
   ID and the override reason, and the application transitions to the appropriate
   final state (approved or declined based on the override decision).

3. **Given** a loan officer attempts to record a human review on an application
   that has not yet reached the pending-decision state,
   **When** the human review is submitted,
   **Then** the system rejects the operation with a typed DomainError identifying
   the invalid state transition, and the application state remains unchanged.

---

### User Story 4 — Compliance Officer: Temporal Compliance Query (Priority: P4)

A compliance officer needs to query the compliance state of a loan application
as it existed at a specific point in time in the past — for example, as it
existed at the moment a decision was made, not as it exists now. This
"regulatory time-travel" capability enables the officer to reconstruct the
exact compliance picture that informed any given decision.

**Why this priority**: Temporal querying is a core regulatory requirement —
examiners must be able to reconstruct the exact state of an application at
any point in time. It is also the technical discriminator between a Score 3
(functional) and Score 4–5 (production-ready) implementation.

**Independent Test**: Can be fully tested by recording a sequence of compliance
events with known timestamps, querying the compliance state at a timestamp
between two events, and verifying the returned state matches only the events
that existed at or before that timestamp.

**Acceptance Scenarios**:

1. **Given** a loan application has had three compliance rules evaluated over
   time — two passing at T1, one failing at T2 — and has since had the failing
   rule remediated and re-passed at T3,
   **When** the compliance state is queried at a timestamp between T2 and T3,
   **Then** the system returns the compliance state as it existed at that moment:
   two rules passed, one rule failed — not the current fully-passed state.

2. **Given** the compliance audit projection is queried for its current lag,
   **When** the lag query is made,
   **Then** the system returns the milliseconds between the latest recorded event
   and the latest event processed by the compliance projection, and this value
   stays below 2,000 ms under normal operating conditions.

3. **Given** the compliance projection has accumulated data across many
   applications and events,
   **When** a rebuild-from-scratch is triggered,
   **Then** the projection table is repopulated by replaying all events from
   the beginning, live reads against the current data continue to be served
   during the rebuild, and the rebuild completes without data loss.

---

### User Story 5 — System Administrator: Monitor Projection Health and Run Integrity Checks (Priority: P5)

A system administrator needs to monitor the health of all projection daemons
(ensuring they are processing events within SLO bounds) and run cryptographic
integrity checks against specific entities in the audit ledger to detect any
tampering or corruption.

**Why this priority**: An unmonitored projection daemon that falls behind SLO
is a silent compliance failure — loan officers and compliance queries silently
return stale data. Integrity checks are the cryptographic proof layer that
makes the audit trail regulatorily defensible.

**Independent Test**: Can be fully tested by querying the health endpoint after
a period of load, verifying all projection lags are within SLO, then directly
modifying a stored event in the database and running an integrity check to confirm
tamper detection.

**Acceptance Scenarios**:

1. **Given** all projections are running and have been processing events,
   **When** the health endpoint is queried,
   **Then** the system returns the current lag for every projection it manages,
   the health query itself completes in under 10 ms at the 99th percentile, and
   each projection's lag value is accurate to within one polling cycle.

2. **Given** an integrity check is requested for entity type "loan" and
   entity ID "app-123",
   **When** the check runs,
   **Then** the system hashes all events for that entity, verifies the hash chain
   against all prior AuditIntegrityCheckRun events, appends a new
   AuditIntegrityCheckRun event with the result, and returns the count of events
   verified, whether the chain is valid, and whether tampering was detected.

3. **Given** an integrity check is requested for the same entity twice within
   one minute (regardless of which caller submits the second request),
   **When** the second request arrives,
   **Then** the system rejects it with a typed rate-limit error indicating when
   the next check will be permitted. No credential validation is performed;
   the rate limit is enforced purely by entity ID and wall-clock time.

---

### User Story 6 — Analyst: What-If Counterfactual Scenario (Priority: P6 — Bonus)

An Apex compliance analyst needs to run counterfactual scenarios to understand
how AI decision outcomes would have changed under different model inputs. For
example: "What would the final loan decision have been if the credit analysis had
assessed the risk tier as HIGH instead of MEDIUM?" The analyst needs to see
the full cascading effect of this substitution through all business rules to the
final decision, without the counterfactual scenario touching the real audit trail.

**Why this priority**: This is the Phase 6 bonus, required only for Score 5.
It demonstrates genuine mastery of event sourcing — the ability to replay
history with modifications while respecting causal dependencies and never
contaminating the real store.

**Independent Test**: Can be fully tested by completing a full lifecycle with
MEDIUM risk tier, running a what-if substituting HIGH risk tier, and verifying
the counterfactual outcome reflects materially different business rule
enforcement (e.g., a different recommendation or credit limit).

**Acceptance Scenarios**:

1. **Given** a completed loan application lifecycle where a credit analysis
   returned risk_tier = MEDIUM and the final decision was APPROVE,
   **When** a what-if scenario is run substituting risk_tier = HIGH at the
   credit analysis branch point,
   **Then** the system returns: the real outcome (APPROVE) and the counterfactual
   outcome (a materially different result due to HIGH risk tier propagating
   through business rules), the list of events that diverged between the two
   scenarios, and zero writes to the real event store.

2. **Given** a what-if scenario is requested with a branch point event that
   has causally dependent downstream events,
   **When** the counterfactual replay runs,
   **Then** causally dependent events (those whose causation_id traces back to
   the branch point or later) are excluded from the counterfactual replay, while
   causally independent events are included, ensuring the counterfactual
   represents a valid alternative history, not a corrupted one.

---

### Edge Cases

- What happens when an application stream is queried before its first event is
  written (stream not yet created)?
- How does the system handle an upcaster that is applied to an event but the
  new field cannot be inferred from available data (null vs. fabricated value)?
- What happens when the projection daemon encounters a malformed event payload
  that cannot be deserialized?
- How does the compliance dependency check behave when the ComplianceRecord
  stream for an application does not yet exist?
- What happens when a human review is submitted for an application where the
  AI orchestrator's decision had confidence_score < 0.6 (auto-REFER)?
- How are archive streams handled in temporal queries — can they still be queried?
- What happens when `generate_regulatory_package` is called for an application
  with no events?

---

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The system MUST record every AI agent action, compliance check,
  human review decision, and final loan outcome as an immutable, append-only
  event in a persistent store.

- **FR-002**: The system MUST enforce optimistic concurrency control on every
  append operation — exactly one of two simultaneous appends to the same stream
  at the same version MUST succeed; the other MUST receive a typed concurrency
  error with reload-and-retry guidance.

- **FR-003**: The system MUST enforce the following six business invariants
  exclusively within aggregate domain logic (not in API or handler layers):
  (a) valid application state machine transitions only,
  (b) agent context declaration required before any agent decision,
  (c) model version locking after first credit analysis,
  (d) confidence floor of 0.6 for non-REFER decisions,
  (e) compliance clearance required before approval,
  (f) causal session references only in orchestrator decisions.

- **FR-004**: The system MUST automatically upcast older event versions to the
  current schema at read time without modifying the stored event payload; the
  raw stored payload MUST be byte-for-byte identical before and after any read.

- **FR-005**: The system MUST maintain three read-optimised projections
  (ApplicationSummary, AgentPerformanceLedger, ComplianceAuditView) updated
  by a background daemon that continues processing even when individual event
  handlers fail.

- **FR-006**: The ComplianceAuditView projection MUST support temporal queries
  returning the compliance state as it existed at any specified past timestamp.

- **FR-007**: The system MUST expose a cryptographic hash chain over the event
  log for any entity, such that any post-hoc modification of a stored event
  payload is detectable.

- **FR-008**: The system MUST enable a crashed AI agent to reconstruct its
  complete working context from the event store alone, within a configurable
  token budget, and MUST flag sessions with unresolved partial decisions as
  requiring reconciliation before new work proceeds.

- **FR-009**: The system MUST expose all write operations as commands and all
  read operations as queries through a structured interface that AI agents can
  consume; all error responses MUST be typed objects with machine-readable
  error types and suggested recovery actions. Identity and role claims
  (`reviewer_id`, `agent_id`, role designations) are caller-supplied parameters
  that the system records verbatim in the event payload for audit purposes;
  the system does NOT validate credentials — access control is a perimeter
  responsibility outside this implementation's scope.

- **FR-010**: The system MUST write events and their outbox routing records in
  the same atomic database transaction, guaranteeing that downstream systems
  are notified of every event exactly once.

- **FR-011**: Every projection daemon MUST expose its current lag in milliseconds;
  the ApplicationSummary lag MUST stay below 500 ms and ComplianceAuditView
  lag MUST stay below 2,000 ms under normal operating conditions.

- **FR-012**: The system MUST support archiving streams (marking them as
  inactive) without deleting their events; archived streams MUST still be
  queryable for audit purposes.

- **FR-013** *(Bonus)*: The system MUST support counterfactual "what-if"
  scenario evaluation by replaying application history with substituted events
  at a specified branch point, excluding causally dependent real events, and
  MUST NOT write counterfactual events to the real store.

- **FR-014** *(Bonus)*: The system MUST produce a self-contained regulatory
  examination package for any application containing the full event stream,
  all projection states at the examination date, integrity verification result,
  human-readable lifecycle narrative, and AI model metadata — verifiable by a
  regulator independently without trusting the system.

### Key Entities

- **LoanApplication**: A commercial loan application progressing through a
  defined state machine from submission to final decision. Consistency boundary:
  all lifecycle events for a single application belong to one stream.

- **AgentSession**: A single AI agent instance's work session on one or more
  applications, including context loading, analysis, and decision events.
  Consistency boundary: every decision MUST reference the session's context
  declaration.

- **ComplianceRecord**: The collection of regulatory checks, rule evaluations,
  and compliance verdicts for a single application. Consistency boundary:
  all checks for one application form one compliance record.

- **AuditLedger**: A cross-cutting, append-only record linking events across
  all aggregates for a single business entity. Consistency boundary: append-only;
  no events may ever be removed.

- **Projection (ApplicationSummary)**: A read-optimised snapshot of every loan
  application's current state. One row per application, updated by the projection
  daemon.

- **Projection (AgentPerformanceLedger)**: Aggregated performance metrics per
  AI agent model version, enabling systematic decision pattern analysis.

- **Projection (ComplianceAuditView)**: A temporally-queryable compliance record
  supporting point-in-time state reconstruction for regulatory examination.

- **IntegrityCheckResult**: The output of a cryptographic hash chain verification
  run, including events verified, chain validity, and tamper detection result.

- **AgentContext**: The reconstructed working context for a crashed AI agent,
  including context text within token budget, last event position, pending work
  items, and session health status (including NEEDS_RECONCILIATION flag).

---

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: An examiner can retrieve the complete decision history of any
  loan application — including all AI agent actions, compliance checks, human
  review, causal links, and cryptographic integrity verification — in under
  60 seconds from query initiation to full result display.

- **SC-002**: Under a load of 50 concurrent loan application workflows, the
  ApplicationSummary view remains within 500 ms of the latest recorded event,
  and the ComplianceAuditView remains within 2,000 ms, as measured by the
  system's own lag metrics.

- **SC-003**: When two agents simultaneously attempt to record conflicting
  decisions on the same application, exactly one succeeds and the total
  event count on the stream increases by exactly one (not two), verified
  across 100% of concurrent collision test runs.

- **SC-004**: A crashed AI agent can resume its session from the Ledger alone —
  without any in-memory state — and correctly identify all pending work items
  and any partial decisions requiring resolution, in 100% of simulated crash
  recovery scenarios.

- **SC-005**: A compliance officer can query the compliance state of any
  application at any past timestamp and receive a result that accurately
  reflects the state at that moment (not the current state), verified by
  querying at a timestamp known to fall between two compliance events with
  different outcomes.

- **SC-006**: Any post-hoc modification of a stored event payload is detected
  by the cryptographic integrity check in 100% of tamper detection test cases.

- **SC-007**: The application read endpoint responds at the 99th percentile
  in under 50 ms; the compliance audit view responds in under 200 ms; the
  health watchdog endpoint responds in under 10 ms — all measured under
  normal operating load.

- **SC-008**: The complete loan application lifecycle — from application
  submission through final decision — can be driven end-to-end using only the
  system's command-and-query interface (no internal function calls), verified
  by a fully automated lifecycle integration test.

- **SC-009**: Loading a version-1 event through the system returns the current
  version-2 schema; directly querying the stored database row confirms the
  payload is byte-for-byte unchanged, verified in 100% of immutability test runs.

- **SC-010** *(Bonus)*: A what-if scenario substituting a HIGH risk tier for
  MEDIUM produces a materially different final decision outcome compared to the
  real history, demonstrating that business rule cascade is correctly applied
  through the counterfactual replay.

---

## Assumptions

- The Apex Financial Services scenario is a faithful proxy for all enterprise
  event sourcing use cases in the regulated-decision space (healthcare, insurance,
  government benefits). Patterns proven here transfer directly.
- A single PostgreSQL instance is the primary store; horizontal scaling of
  the write path is out of scope for this implementation (documented in DESIGN.md
  as a future consideration).
- The four aggregates (LoanApplication, AgentSession, ComplianceRecord,
  AuditLedger) and their stream ID formats are fixed by the challenge specification
  and are not subject to change.
- Phase 6 (what-if projections and regulatory package) is attempted only after
  Phases 1–5 are fully functional; it is required for Score 5 but not Score 3.
- The video demonstration is a non-negotiable deliverable alongside the code;
  a passing code submission without a compliant demo is not considered complete.
