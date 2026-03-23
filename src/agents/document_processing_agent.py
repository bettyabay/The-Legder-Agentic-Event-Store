from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypedDict

from langgraph.graph import END, StateGraph

from src.agents.base_agent import BaseApexAgent


class DocProcState(TypedDict):
    application_id: str
    session_id: str
    agent_id: str
    applicant_id: str | None
    uploaded_documents: list[dict[str, Any]]
    extraction_results: list[dict[str, Any]]
    quality_assessment: dict[str, Any] | None
    output_events_written: list[dict[str, Any]]
    next_agent_triggered: str | None


class DocumentProcessingAgent(BaseApexAgent):
    """Week-5 document processing agent with quality LLM check."""

    def build_graph(self) -> Any:
        graph = StateGraph(DocProcState)
        graph.add_node("validate_inputs", self._node_validate_inputs)
        graph.add_node("validate_document_formats", self._node_validate_formats)
        graph.add_node("extract_income_statement", self._node_extract_income)
        graph.add_node("extract_balance_sheet", self._node_extract_balance)
        graph.add_node("assess_quality", self._node_assess_quality)
        graph.add_node("write_output", self._node_write_output)

        graph.set_entry_point("validate_inputs")
        graph.add_edge("validate_inputs", "validate_document_formats")
        graph.add_edge("validate_document_formats", "extract_income_statement")
        graph.add_edge("extract_income_statement", "extract_balance_sheet")
        graph.add_edge("extract_balance_sheet", "assess_quality")
        graph.add_edge("assess_quality", "write_output")
        graph.add_edge("write_output", END)
        return graph.compile()

    def _initial_state(self) -> dict[str, Any]:
        state = super()._initial_state()
        state.update(
            {
                "applicant_id": None,
                "uploaded_documents": [],
                "extraction_results": [],
                "quality_assessment": None,
                "output_events_written": [],
                "next_agent_triggered": None,
            }
        )
        return state

    async def _node_validate_inputs(self, state: DocProcState) -> DocProcState:
        t0 = time.time()
        app_id = state["application_id"]
        loan_events = await self.store.load_stream(f"loan-{app_id}")
        submitted = next((e for e in loan_events if e.event_type == "ApplicationSubmitted"), None)
        if submitted is None:
            raise ValueError(f"ApplicationSubmitted not found for {app_id}")
        docs = [e.payload for e in loan_events if e.event_type == "DocumentUploaded"]
        # Real-world fallback: if documents exist on disk but upload events were not
        # recorded yet, discover and backfill DocumentUploaded so downstream nodes work.
        if not docs:
            applicant_id = str(submitted.payload.get("applicant_id", ""))
            discovered = self._discover_docs_on_disk(app_id, applicant_id)
            if discovered:
                current_ver = await self.store.stream_version(f"loan-{app_id}")
                now_iso = datetime.now(timezone.utc).isoformat()
                upload_events: list[dict[str, Any]] = []
                for i, d in enumerate(discovered, start=1):
                    upload_events.append(
                        {
                            "event_type": "DocumentUploaded",
                            "event_version": 1,
                            "payload": {
                                "application_id": app_id,
                                "document_id": d["document_id"] or f"doc-auto-{i}",
                                "document_type": d["document_type"],
                                "document_format": d["document_format"],
                                "filename": d["filename"],
                                "file_path": d["file_path"],
                                "file_size_bytes": d["file_size_bytes"],
                                "file_hash": d["file_hash"],
                                "fiscal_year": d["fiscal_year"],
                                "uploaded_at": now_iso,
                                "uploaded_by": "system:auto_discovery",
                            },
                        }
                    )
                await self.store.append(
                    f"loan-{app_id}",
                    upload_events,
                    expected_version=current_ver,
                )
                loan_events = await self.store.load_stream(f"loan-{app_id}")
                docs = [e.payload for e in loan_events if e.event_type == "DocumentUploaded"]
        if not docs:
            raise ValueError(f"No DocumentUploaded events found for {app_id}")
        await self._record_node_execution(
            node_name="validate_inputs",
            input_keys=["application_id"],
            output_keys=["applicant_id", "uploaded_documents"],
            duration_ms=int((time.time() - t0) * 1000),
        )
        return {**state, "applicant_id": str(submitted.payload.get("applicant_id", "")), "uploaded_documents": docs}

    def _discover_docs_on_disk(self, app_id: str, applicant_id: str) -> list[dict[str, Any]]:
        """Best-effort discovery of application documents from local workspace."""
        roots: list[Path] = []
        explicit = os.environ.get("LEDGER_DOCUMENT_PATHS", "")
        explicit_roots: set[str] = set()
        if explicit:
            for raw in explicit.split(os.pathsep):
                candidate = raw.strip()
                if candidate:
                    p = Path(candidate)
                    roots.append(p)
                    explicit_roots.add(str(p.resolve()))
        roots.extend([Path("documents"), Path("artifacts"), Path(".")])
        prefixes = [f"{app_id}_", app_id]
        found: list[dict[str, Any]] = []
        seen_paths: set[str] = set()

        for root in roots:
            if not root.exists():
                continue
            search_root = root / applicant_id if root.is_dir() and applicant_id and (root / applicant_id).exists() else root
            is_explicit_root = str(root.resolve()) in explicit_roots
            if search_root.is_file():
                candidates = [search_root]
            else:
                candidates = list(search_root.rglob("*"))
            for p in candidates:
                if not p.is_file():
                    continue
                name = p.name
                # Explicit paths are user-selected inputs and should not require
                # app-id-prefixed filenames.
                if not is_explicit_root and not any(name.startswith(pref) for pref in prefixes):
                    continue
                ext = p.suffix.lower().lstrip(".")
                if ext not in {"pdf", "xlsx", "xls", "csv"}:
                    continue
                normalized = str(p).replace("\\", "/")
                if normalized in seen_paths:
                    continue
                seen_paths.add(normalized)
                lower = name.lower()
                if "balance" in lower:
                    doc_type = "balance_sheet"
                elif "income" in lower or "financial" in lower:
                    doc_type = "income_statement"
                else:
                    doc_type = "application_proposal"
                found.append(
                    {
                        "document_id": f"doc-auto-{abs(hash(normalized)) % (10**8):08d}",
                        "document_type": doc_type,
                        "document_format": ext,
                        "filename": name,
                        "file_path": normalized,
                        "file_size_bytes": p.stat().st_size,
                        "file_hash": "",
                        "fiscal_year": 2024 if "2024" in lower else None,
                    }
                )
        return found

    async def _node_validate_formats(self, state: DocProcState) -> DocProcState:
        t0 = time.time()
        app_id = state["application_id"]
        docs = state.get("uploaded_documents") or []
        valid_docs: list[dict[str, Any]] = []
        for d in docs:
            path = str(d.get("file_path") or "")
            doc_id = str(d.get("document_id") or "unknown")
            doc_type = str(d.get("document_type") or "unknown")
            if path and os.path.exists(path):
                page_count = 1
                ext = os.path.splitext(path)[1].lower().lstrip(".") or "unknown"
                await self._append_docpkg(
                    app_id,
                    [
                        {
                            "event_type": "DocumentFormatValidated",
                            "event_version": 1,
                            "payload": {
                                "package_id": app_id,
                                "document_id": doc_id,
                                "document_type": doc_type,
                                "page_count": page_count,
                                "detected_format": ext,
                                "validated_at": datetime.now(timezone.utc).isoformat(),
                            },
                        }
                    ],
                )
                valid_docs.append(d)
        ms = int((time.time() - t0) * 1000)
        await self._record_node_execution(
            node_name="validate_document_formats",
            input_keys=["uploaded_documents"],
            output_keys=["uploaded_documents"],
            duration_ms=ms,
        )
        return {**state, "uploaded_documents": valid_docs}

    async def _node_extract_income(self, state: DocProcState) -> DocProcState:
        return await self._extract_for_doc_type(state, "income_statement")

    async def _node_extract_balance(self, state: DocProcState) -> DocProcState:
        return await self._extract_for_doc_type(state, "balance_sheet")

    async def _extract_for_doc_type(self, state: DocProcState, doc_type: str) -> DocProcState:
        t0 = time.time()
        app_id = state["application_id"]
        docs = state.get("uploaded_documents") or []
        target = next((d for d in docs if str(d.get("document_type")) == doc_type), None)
        if target is None:
            await self._record_node_execution(
                node_name=f"extract_{doc_type}",
                input_keys=["uploaded_documents"],
                output_keys=["extraction_results"],
                duration_ms=int((time.time() - t0) * 1000),
            )
            return state

        applicant_id = state.get("applicant_id")
        if not applicant_id:
            raise ValueError("Missing applicant_id")
        financials = await self.registry.get_financial_history(applicant_id)
        latest = financials[-1] if financials else None
        facts: dict[str, Any] = {}
        if latest is not None:
            ld = latest.__dict__
            fields = [
                "total_revenue",
                "gross_profit",
                "operating_expenses",
                "operating_income",
                "ebitda",
                "net_income",
                "total_assets",
                "total_liabilities",
                "total_equity",
                "current_assets",
                "current_liabilities",
            ]
            facts = {k: (str(ld.get(k)) if ld.get(k) is not None else None) for k in fields}

        doc_id = str(target.get("document_id") or "unknown")
        started = {
            "event_type": "ExtractionStarted",
            "event_version": 1,
            "payload": {
                "package_id": app_id,
                "document_id": doc_id,
                "document_type": doc_type,
                "pipeline_version": "week3-v1.0",
                "extraction_model": "registry-backed-v1",
                "started_at": datetime.now(timezone.utc).isoformat(),
            },
        }
        completed = {
            "event_type": "ExtractionCompleted",
            "event_version": 1,
            "payload": {
                "package_id": app_id,
                "document_id": doc_id,
                "document_type": doc_type,
                "facts": facts,
                "raw_text_length": 0,
                "tables_extracted": 1,
                "processing_ms": int((time.time() - t0) * 1000),
                "completed_at": datetime.now(timezone.utc).isoformat(),
            },
        }
        await self._append_docpkg(app_id, [started, completed])

        ms = int((time.time() - t0) * 1000)
        await self._record_tool_call(
            tool_name="query_applicant_registry",
            tool_input_summary=f"company_id={applicant_id} for {doc_type}",
            tool_output_summary=f"financial_rows={len(financials)}",
            tool_duration_ms=ms,
        )
        node_name = "extract_income_statement" if doc_type == "income_statement" else "extract_balance_sheet"
        await self._record_node_execution(
            node_name=node_name,
            input_keys=["uploaded_documents", "applicant_id"],
            output_keys=["extraction_results"],
            duration_ms=ms,
        )

        results = list(state.get("extraction_results") or [])
        results.append(completed["payload"])
        return {**state, "extraction_results": results}

    async def _node_assess_quality(self, state: DocProcState) -> DocProcState:
        t0 = time.time()
        results = state.get("extraction_results") or []
        facts = [r.get("facts") for r in results]

        system = (
            "You are a financial document quality analyst. Return ONLY JSON with keys: "
            "overall_confidence(float), is_coherent(bool), anomalies(array), critical_missing_fields(array), "
            "reextraction_recommended(bool), auditor_notes(string)."
        )
        user = f"Extracted facts: {json.dumps(facts, default=str)[:2500]}"
        llm = await self._call_llm(system, user, max_tokens=400)
        try:
            match = re.search(r"\{.*\}", llm.text, re.DOTALL)
            qa = json.loads(match.group(0) if match else "{}")
        except json.JSONDecodeError:
            qa = {}
        qa.setdefault("overall_confidence", 0.7)
        qa.setdefault("is_coherent", True)
        qa.setdefault("anomalies", [])
        qa.setdefault("critical_missing_fields", [])
        qa.setdefault("reextraction_recommended", False)
        qa.setdefault("auditor_notes", "Quality assessed.")

        app_id = state["application_id"]
        await self._append_docpkg(
            app_id,
            [
                {
                    "event_type": "QualityAssessmentCompleted",
                    "event_version": 1,
                    "payload": {
                        "package_id": app_id,
                        "document_id": f"qa-{self.session_id}",
                        "overall_confidence": qa["overall_confidence"],
                        "is_coherent": qa["is_coherent"],
                        "anomalies": qa["anomalies"],
                        "critical_missing_fields": qa["critical_missing_fields"],
                        "reextraction_recommended": qa["reextraction_recommended"],
                        "auditor_notes": qa["auditor_notes"],
                        "assessed_at": datetime.now(timezone.utc).isoformat(),
                    },
                }
            ],
        )
        await self._record_node_execution(
            node_name="assess_quality",
            input_keys=["extraction_results"],
            output_keys=["quality_assessment"],
            duration_ms=int((time.time() - t0) * 1000),
            llm_called=True,
            llm_tokens_input=llm.input_tokens,
            llm_tokens_output=llm.output_tokens,
            llm_cost_usd=llm.cost_usd,
        )
        return {**state, "quality_assessment": qa}

    async def _node_write_output(self, state: DocProcState) -> DocProcState:
        t0 = time.time()
        app_id = state["application_id"]
        qa = state.get("quality_assessment") or {}
        docs_processed = len(state.get("extraction_results") or [])

        pkg_events = [
            {
                "event_type": "PackageReadyForAnalysis",
                "event_version": 1,
                "payload": {
                    "package_id": app_id,
                    "application_id": app_id,
                    "documents_processed": docs_processed,
                    "has_quality_flags": bool(qa.get("anomalies") or qa.get("critical_missing_fields")),
                    "quality_flag_count": len(qa.get("anomalies") or []) + len(qa.get("critical_missing_fields") or []),
                    "ready_at": datetime.now(timezone.utc).isoformat(),
                },
            }
        ]
        pkg_ver = await self._append_docpkg(app_id, pkg_events)

        loan_stream = f"loan-{app_id}"
        loan_ver = await self.store.stream_version(loan_stream)
        loan_ver2 = await self.store.append(
            loan_stream,
            [
                {
                    "event_type": "CreditAnalysisRequested",
                    "event_version": 1,
                    "payload": {
                        "application_id": app_id,
                        "requested_at": datetime.now(timezone.utc).isoformat(),
                        "requested_by": f"document_agent:{self.session_id}",
                        "priority": "NORMAL",
                    },
                }
            ],
            expected_version=loan_ver,
        )
        events_written = [
            {"stream_id": f"docpkg-{app_id}", "event_type": "PackageReadyForAnalysis", "stream_position": pkg_ver},
            {"stream_id": loan_stream, "event_type": "CreditAnalysisRequested", "stream_position": loan_ver2},
        ]
        await self._record_output_written(events_written=events_written, output_summary=f"Document processing completed for {app_id}.")
        await self._record_node_execution(
            node_name="write_output",
            input_keys=["quality_assessment", "extraction_results"],
            output_keys=["output_events_written", "next_agent_triggered"],
            duration_ms=int((time.time() - t0) * 1000),
        )
        return {**state, "output_events_written": events_written, "next_agent_triggered": "credit_analysis"}

    async def _append_docpkg(self, app_id: str, events: list[dict[str, Any]]) -> int:
        stream_id = f"docpkg-{app_id}"
        ver = await self.store.stream_version(stream_id)
        expected = -1 if ver == 0 else ver
        return await self.store.append(stream_id, events, expected_version=expected)
