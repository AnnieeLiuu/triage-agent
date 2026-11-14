"""FastMCP server providing read-only observability tools and gated eval-case capture."""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

load_dotenv()

mcp = FastMCP("pickforme-ops")

DEFAULT_EVAL_CASES_DIR = Path("evals/cases")

TOOL_PERMISSIONS: dict[str, str] = {
    "fetch_failed_traces": "READ",
    "get_trace_spans": "READ",
    "check_neon_health": "READ",
    "check_llm_providers": "READ",
    "save_eval_case": "WRITE",
}


def _get_langfuse_client() -> Any:
    """Create a Langfuse client from environment variables, or return None if unconfigured."""
    public_key = os.getenv("LANGFUSE_PUBLIC_KEY", "").strip()
    secret_key = os.getenv("LANGFUSE_SECRET_KEY", "").strip()
    host = os.getenv("LANGFUSE_HOST", "https://cloud.langfuse.com").strip()
    if not public_key or not secret_key:
        return None
    from langfuse import Langfuse

    return Langfuse(public_key=public_key, secret_key=secret_key, host=host)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def fetch_failed_traces(limit: int = 10, min_score: float | None = None) -> list[dict[str, Any]]:
    """Fetch recent failed or low-scoring Langfuse traces for PickForMe."""
    client = _get_langfuse_client()
    if client is None:
        return [{"error": "LANGFUSE_PUBLIC_KEY or LANGFUSE_SECRET_KEY is not configured."}]

    try:
        response = client.api.trace.list(limit=max(1, min(limit, 50)))
        traces = getattr(response, "data", response) or []
    except Exception as exc:
        return [{"error": f"Failed to fetch Langfuse traces: {exc}"}]

    results: list[dict[str, Any]] = []
    for trace in traces:
        trace_id = getattr(trace, "id", None) or (trace.get("id") if isinstance(trace, dict) else None)
        name = getattr(trace, "name", None) or (trace.get("name") if isinstance(trace, dict) else None)
        level = getattr(trace, "level", None) or (trace.get("level") if isinstance(trace, dict) else None)
        status_msg = getattr(trace, "status_message", None) or (
            trace.get("status_message") if isinstance(trace, dict) else None
        )
        scores = getattr(trace, "scores", None) or (trace.get("scores") if isinstance(trace, dict) else []) or []

        numeric_scores = [
            float(getattr(s, "value", s.get("value") if isinstance(s, dict) else 0.0))
            for s in scores
            if (getattr(s, "value", None) is not None or (isinstance(s, dict) and s.get("value") is not None))
        ]
        lowest_score = min(numeric_scores) if numeric_scores else None
        is_error = str(level).upper() == "ERROR" or bool(status_msg)
        is_low_score = min_score is not None and lowest_score is not None and lowest_score <= min_score

        if is_error or is_low_score or min_score is None:
            results.append(
                {
                    "trace_id": trace_id,
                    "name": name,
                    "level": level,
                    "status_message": status_msg,
                    "lowest_score": lowest_score,
                }
            )
    return results


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def get_trace_spans(trace_id: str) -> dict[str, Any]:
    """Fetch detailed spans/observations for a specific Langfuse trace_id."""
    client = _get_langfuse_client()
    if client is None:
        return {"error": "LANGFUSE_PUBLIC_KEY or LANGFUSE_SECRET_KEY is not configured."}

    try:
        trace = client.api.trace.get(trace_id)
        observations = getattr(trace, "observations", None) or []
        spans = []
        for obs in observations:
            spans.append(
                {
                    "id": getattr(obs, "id", None),
                    "name": getattr(obs, "name", None),
                    "type": getattr(obs, "type", None),
                    "level": getattr(obs, "level", None),
                    "status_message": getattr(obs, "status_message", None),
                    "start_time": str(getattr(obs, "start_time", "")),
                    "end_time": str(getattr(obs, "end_time", "")),
                }
            )
        return {
            "trace_id": trace_id,
            "name": getattr(trace, "name", None),
            "span_count": len(spans),
            "spans": spans,
        }
    except Exception as exc:
        return {"trace_id": trace_id, "error": f"Failed to fetch trace spans: {exc}"}


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def check_neon_health() -> dict[str, Any]:
    """Check Neon Serverless Postgres connectivity and SSL configuration (read-only SELECT 1)."""
    dsn = os.getenv("DATABASE_URL", "").strip()
    if not dsn:
        return {"ok": False, "ssl_configured": False, "error": "DATABASE_URL is not configured."}

    ssl_configured = "sslmode=require" in dsn
    start = time.perf_counter()
    try:
        import psycopg

        with psycopg.connect(dsn, connect_timeout=5) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
        latency_ms = round((time.perf_counter() - start) * 1000, 2)
        return {"ok": True, "latency_ms": latency_ms, "ssl_configured": ssl_configured, "error": None}
    except Exception as exc:
        latency_ms = round((time.perf_counter() - start) * 1000, 2)
        return {"ok": False, "latency_ms": latency_ms, "ssl_configured": ssl_configured, "error": str(exc)}


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def check_llm_providers() -> dict[str, Any]:
    """Check configuration and basic reachability of configured LLM providers."""
    gemini_configured = bool(os.getenv("GEMINI_API_KEY", "").strip())
    groq_configured = bool(os.getenv("GROQ_API_KEY", "").strip())
    ollama_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434").strip().rstrip("/")

    ollama_reachable = False
    ollama_error: str | None = None
    try:
        resp = httpx.get(f"{ollama_url}/api/tags", timeout=2.0)
        ollama_reachable = resp.status_code == 200
    except Exception as exc:
        ollama_error = str(exc)

    return {
        "gemini": {"configured": gemini_configured, "model": os.getenv("GEMINI_MODEL", "gemini-2.5-flash")},
        "groq": {"configured": groq_configured},
        "ollama": {"base_url": ollama_url, "reachable": ollama_reachable, "error": ollama_error},
    }


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False))
def save_eval_case(
    case_id: str,
    title: str,
    root_cause: str,
    trace_id: str | None = None,
    reproduction_input: dict[str, Any] | None = None,
    expected_behavior: str = "",
    cases_dir: str | None = None,
) -> dict[str, Any]:
    """Save a verified incident root cause as a structured JSON eval case in evals/cases/."""
    safe_id = re.sub(r"[^a-zA-Z0-9_-]+", "-", case_id).strip("-")
    if not safe_id:
        raise ValueError("case_id must contain at least one alphanumeric character.")

    target_dir = Path(cases_dir) if cases_dir else DEFAULT_EVAL_CASES_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    out_path = target_dir / f"{safe_id}.json"

    payload = {
        "case_id": safe_id,
        "title": title,
        "root_cause": root_cause,
        "trace_id": trace_id,
        "reproduction_input": reproduction_input or {},
        "expected_behavior": expected_behavior,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"saved": True, "path": str(out_path), "case_id": safe_id}


if __name__ == "__main__":
    mcp.run(transport="stdio")
