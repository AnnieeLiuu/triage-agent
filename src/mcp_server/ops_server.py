"""FastMCP server providing read-only observability tools and gated eval-case capture."""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
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
    "get_recent_agent_runs": "READ",
    "check_llm_providers": "READ",
    "save_eval_case": "WRITE",
}


def _get_langfuse_auth() -> tuple[str, str, str] | None:
    """Return (public_key, secret_key, host) from env, or None if unconfigured."""
    public_key = os.getenv("LANGFUSE_PUBLIC_KEY", "").strip()
    secret_key = os.getenv("LANGFUSE_SECRET_KEY", "").strip()
    host = os.getenv("LANGFUSE_HOST", "https://cloud.langfuse.com").strip().rstrip("/")
    if not public_key or not secret_key:
        return None
    return public_key, secret_key, host


def _truncate_io(value: Any, max_chars: int = 1500) -> Any:
    """Truncate large observation input/output payloads to protect LLM context."""
    if value is None:
        return None
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    if len(text) <= max_chars:
        return value
    return text[:max_chars] + f"... [truncated {len(text) - max_chars} chars]"


def _fetch_observations(params: dict[str, Any]) -> list[dict[str, Any]]:
    """Query Langfuse v2 observations API with explicit timeout."""
    auth_info = _get_langfuse_auth()
    if auth_info is None:
        raise ValueError("LANGFUSE_PUBLIC_KEY or LANGFUSE_SECRET_KEY is not configured.")
    public_key, secret_key, host = auth_info
    resp = httpx.get(
        f"{host}/api/public/v2/observations",
        params=params,
        auth=(public_key, secret_key),
        timeout=10.0,
    )
    resp.raise_for_status()
    payload = resp.json()
    return list(payload.get("data", []))


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def fetch_failed_traces(limit: int = 10, days: int = 7, only_errors: bool = False) -> list[dict[str, Any]]:
    """Fetch recent Langfuse traces (with user input preview) or filter server-side for ERROR level."""
    if _get_langfuse_auth() is None:
        return [{"error": "LANGFUSE_PUBLIC_KEY or LANGFUSE_SECRET_KEY is not configured."}]

    now = datetime.now(timezone.utc)
    from_iso = (now - timedelta(days=max(1, days))).isoformat().replace("+00:00", "Z")
    to_iso = now.isoformat().replace("+00:00", "Z")

    params: dict[str, Any] = {
        "fields": "core,basic,io",
        "fromStartTime": from_iso,
        "toStartTime": to_iso,
        "limit": max(1, min(limit * 5, 100)),
    }
    if only_errors:
        params["level"] = "ERROR"

    try:
        observations = _fetch_observations(params)
    except Exception as exc:
        return [{"error": f"Failed to fetch Langfuse observations: {exc}"}]

    grouped: dict[str, dict[str, Any]] = {}
    for obs in observations:
        trace_id = obs.get("traceId")
        if not trace_id:
            continue
        level = str(obs.get("level") or "DEFAULT").upper()
        status_msg = obs.get("statusMessage") or ""
        is_error = level == "ERROR" or bool(status_msg)

        entry = grouped.setdefault(
            trace_id,
            {
                "trace_id": trace_id,
                "name": obs.get("name"),
                "level": level,
                "status_message": status_msg,
                "start_time": obs.get("startTime"),
                "input_preview": _truncate_io(obs.get("input"), max_chars=300),
                "output_preview": _truncate_io(obs.get("output"), max_chars=300),
                "observation_count": 0,
                "has_error": False,
            },
        )
        entry["observation_count"] += 1
        # Prefer root observation's input/output when available
        if obs.get("parentObservationId") is None and obs.get("input") is not None:
            entry["input_preview"] = _truncate_io(obs.get("input"), max_chars=300)
            entry["output_preview"] = _truncate_io(obs.get("output"), max_chars=300)
            entry["name"] = obs.get("name") or entry["name"]
        if is_error:
            entry["has_error"] = True
            entry["level"] = "ERROR"
            if status_msg:
                entry["status_message"] = status_msg

    traces = list(grouped.values())
    if only_errors:
        traces = [t for t in traces if t["has_error"]]
    return traces[: max(1, min(limit, 50))]


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def get_trace_spans(trace_id: str, days: int = 7) -> dict[str, Any]:
    """Fetch detailed spans/observations for a specific Langfuse trace_id."""
    if _get_langfuse_auth() is None:
        return {"error": "LANGFUSE_PUBLIC_KEY or LANGFUSE_SECRET_KEY is not configured."}

    now = datetime.now(timezone.utc)
    from_iso = (now - timedelta(days=max(1, days))).isoformat().replace("+00:00", "Z")
    to_iso = now.isoformat().replace("+00:00", "Z")

    try:
        observations = _fetch_observations(
            {
                "fields": "core,basic,io,metadata,usage",
                "traceId": trace_id,
                "fromStartTime": from_iso,
                "toStartTime": to_iso,
                "limit": 50,
            }
        )
    except Exception as exc:
        return {"trace_id": trace_id, "error": f"Failed to fetch trace spans: {exc}"}

    observations.sort(key=lambda obs: str(obs.get("startTime") or ""))
    spans = [
        {
            "id": obs.get("id"),
            "parent_observation_id": obs.get("parentObservationId"),
            "name": obs.get("name"),
            "type": obs.get("type"),
            "level": obs.get("level"),
            "status_message": obs.get("statusMessage"),
            "latency": obs.get("latency"),
            "start_time": obs.get("startTime"),
            "end_time": obs.get("endTime"),
            "input": _truncate_io(obs.get("input"), max_chars=2000),
            "output": _truncate_io(obs.get("output"), max_chars=2000),
            "usage": {
                "input": obs.get("inputUsage"),
                "output": obs.get("outputUsage"),
                "total": obs.get("totalUsage"),
                "details": obs.get("usageDetails"),
            },
        }
        for obs in observations
    ]
    return {
        "trace_id": trace_id,
        "span_count": len(spans),
        "spans": spans,
    }


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
def get_recent_agent_runs(limit: int = 5, run_id: str | None = None) -> list[dict[str, Any]]:
    """Query recent PickForMe agent_runs and their agent_steps from Neon Postgres (read-only)."""
    dsn = os.getenv("DATABASE_URL", "").strip()
    if not dsn:
        return [{"error": "DATABASE_URL is not configured."}]

    safe_limit = max(1, min(limit, 20))
    try:
        import psycopg

        with psycopg.connect(dsn, connect_timeout=5) as conn:
            with conn.cursor() as cur:
                if run_id:
                    cur.execute(
                        """
                        SELECT run_id, chat_id, user_query, model_used, total_tokens, latency_ms, status, created_at
                        FROM agent_runs
                        WHERE run_id = %s
                        """,
                        (run_id,),
                    )
                else:
                    cur.execute(
                        """
                        SELECT run_id, chat_id, user_query, model_used, total_tokens, latency_ms, status, created_at
                        FROM agent_runs
                        ORDER BY created_at DESC
                        LIMIT %s
                        """,
                        (safe_limit,),
                    )
                run_rows = cur.fetchall()
                if not run_rows:
                    return []

                run_ids = [row[0] for row in run_rows]
                cur.execute(
                    """
                    SELECT run_id, step_number, action_type, tool_name, tool_input, tool_output,
                           latency_ms, error_message, created_at
                    FROM agent_steps
                    WHERE run_id = ANY(%s)
                    ORDER BY step_number ASC, created_at ASC
                    """,
                    (run_ids,),
                )
                step_rows = cur.fetchall()
    except Exception as exc:
        return [{"error": f"Failed to query agent_runs: {exc}"}]

    steps_by_run: dict[str, list[dict[str, Any]]] = {}
    for s_row in step_rows:
        rid = str(s_row[0])
        steps_by_run.setdefault(rid, []).append(
            {
                "step_number": s_row[1],
                "action_type": s_row[2],
                "tool_name": s_row[3],
                "tool_input": s_row[4],
                "tool_output": s_row[5],
                "latency_ms": s_row[6],
                "error_message": s_row[7],
                "created_at": s_row[8].isoformat() if s_row[8] else None,
            }
        )

    results: list[dict[str, Any]] = []
    for r_row in run_rows:
        rid = str(r_row[0])
        results.append(
            {
                "run_id": rid,
                "chat_id": r_row[1],
                "user_query": r_row[2],
                "model_used": r_row[3],
                "total_tokens": r_row[4],
                "latency_ms": r_row[5],
                "status": r_row[6],
                "created_at": r_row[7].isoformat() if r_row[7] else None,
                "steps": steps_by_run.get(rid, []),
            }
        )
    return results


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
        "gemini": {"configured": gemini_configured, "model": os.getenv("GEMINI_MODEL", "gemini-3-flash-preview")},
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
) -> dict[str, Any]:
    """Save a verified incident root cause as a structured JSON eval case in evals/cases/."""
    safe_id = re.sub(r"[^a-zA-Z0-9_-]+", "-", case_id).strip("-")
    if not safe_id:
        raise ValueError("case_id must contain at least one alphanumeric character.")

    env_dir = os.getenv("TRIAGE_EVAL_CASES_DIR", "").strip()
    target_dir = Path(env_dir) if env_dir else DEFAULT_EVAL_CASES_DIR
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
