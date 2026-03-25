param(
    [Parameter(Mandatory = $true)]
    [string]$ApplicationId,
    [string]$Model = "claude-sonnet-4-20250514",
    [switch]$AutoSubmitIfMissing
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $repoRoot

function Invoke-Section {
    param([string]$Title)
    Write-Host ""
    Write-Host ("=" * 88)
    Write-Host $Title
    Write-Host ("=" * 88)
}

function Invoke-SqlChecks {
    param([string]$Label)
    Invoke-Section "SQL CHECKPOINT :: $Label :: app=$ApplicationId"

    $env:LEDGER_STEPWISE_APP_ID = $ApplicationId
    @'
import asyncio
import os
from src.db.pool import create_pool

APP_ID = os.environ["LEDGER_STEPWISE_APP_ID"]
STREAM_ID = f"loan-{APP_ID}"

async def main() -> None:
    pool = await create_pool()
    try:
        async with pool.acquire() as conn:
            stream = await conn.fetchrow(
                """
                SELECT stream_id, current_version, created_at, archived_at
                FROM event_streams
                WHERE stream_id = $1
                """,
                STREAM_ID,
            )
            if stream is None:
                print(f"stream={STREAM_ID} :: not found")
                return

            print(
                f"stream={stream['stream_id']} version={stream['current_version']} "
                f"created_at={stream['created_at']} archived_at={stream['archived_at']}"
            )

            by_type = await conn.fetch(
                """
                SELECT event_type, COUNT(*) AS n
                FROM events
                WHERE payload->>'application_id' = $1
                GROUP BY event_type
                ORDER BY event_type
                """,
                APP_ID,
            )
            print("events_by_type_for_application:")
            if not by_type:
                print("  (none)")
            for row in by_type:
                print(f"  - {row['event_type']}: {row['n']}")

            latest = await conn.fetch(
                """
                SELECT global_position, stream_id, stream_position, event_type, recorded_at
                FROM events
                WHERE payload->>'application_id' = $1
                ORDER BY global_position DESC
                LIMIT 8
                """,
                APP_ID,
            )
            print("latest_events_for_application:")
            if not latest:
                print("  (none)")
            for row in latest:
                print(
                    f"  gp={row['global_position']} stream={row['stream_id']} "
                    f"sp={row['stream_position']} type={row['event_type']} at={row['recorded_at']}"
                )
    finally:
        await pool.close()

asyncio.run(main())
'@ | uv run python -
}

function Ensure-ApplicationSubmitted {
    $env:LEDGER_STEPWISE_APP_ID = $ApplicationId
    $exists = (@'
import asyncio
import os
from src.db.pool import create_pool

APP_ID = os.environ["LEDGER_STEPWISE_APP_ID"]
STREAM_ID = f"loan-{APP_ID}"

async def main() -> None:
    pool = await create_pool()
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT 1
                FROM events
                WHERE stream_id = $1 AND event_type = 'ApplicationSubmitted'
                LIMIT 1
                """,
                STREAM_ID,
            )
            print("yes" if row else "no")
    finally:
        await pool.close()

asyncio.run(main())
'@ | uv run python -).Trim()

    if ($exists -eq "yes") {
        Write-Host "Prerequisite OK: ApplicationSubmitted exists for loan-$ApplicationId"
        return
    }

    if (-not $AutoSubmitIfMissing) {
        throw "Missing ApplicationSubmitted for loan-$ApplicationId. Re-run with -AutoSubmitIfMissing or seed manually."
    }

    Write-Host "No ApplicationSubmitted found. Appending one bootstrap event..."
    @'
import asyncio
import os
from datetime import datetime, timezone
from src.db.pool import create_pool, run_migrations
from src.event_store import EventStore
from src.models.events import ApplicationSubmitted

APP_ID = os.environ["LEDGER_STEPWISE_APP_ID"]
STREAM_ID = f"loan-{APP_ID}"

async def main() -> None:
    pool = await create_pool()
    try:
        await run_migrations(pool)
        store = EventStore(pool=pool)
        await store.append(
            stream_id=STREAM_ID,
            events=[
                ApplicationSubmitted(
                    application_id=APP_ID,
                    applicant_id=f"applicant-{APP_ID}",
                    requested_amount_usd=250000.0,
                    loan_purpose="Working capital",
                    submission_channel="manual-bootstrap",
                    submitted_at=datetime.now(timezone.utc),
                )
            ],
            expected_version=-1,
            correlation_id=f"bootstrap-{APP_ID}",
        )
        print(f"Bootstrapped ApplicationSubmitted for {APP_ID}")
    finally:
        await pool.close()

asyncio.run(main())
'@ | uv run python -
}

Invoke-Section "Stepwise Agent Runner"
Write-Host "Repository root: $repoRoot"
Write-Host "Application ID : $ApplicationId"
Write-Host "Model          : $Model"
Write-Host "Auto-submit    : $AutoSubmitIfMissing"

Invoke-Section "Running migrations"
uv run python scripts/run_migrations.py

Ensure-ApplicationSubmitted
Invoke-SqlChecks -Label "Before agents"

$steps = @(
    @{ Name = "DocumentProcessingAgent"; Script = "scripts/run_document_agent.py" },
    @{ Name = "CreditAnalysisAgent"; Script = "scripts/run_credit_agent.py" },
    @{ Name = "FraudDetectionAgent"; Script = "scripts/run_fraud_agent.py" },
    @{ Name = "ComplianceAgent"; Script = "scripts/run_compliance_agent.py" },
    @{ Name = "DecisionOrchestratorAgent"; Script = "scripts/run_decision_agent.py" }
)

foreach ($step in $steps) {
    Read-Host ("Press Enter for next agent: " + $step.Name)
    Invoke-Section ("Running " + $step.Name)
    uv run python $step.Script --application-id $ApplicationId --model $Model
    Invoke-SqlChecks -Label ("After " + $step.Name)
}

Invoke-Section "Final history/proof view"
uv run python scripts/query_history.py --application-id $ApplicationId

Write-Host ""
Write-Host "Stepwise run complete."
