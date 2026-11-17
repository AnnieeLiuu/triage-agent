"""Langfuse Telemetry facade with null-object fallback (zero network calls during test)."""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


class NoOpSpan:
    """Null-object pattern for Span/Trace when Langfuse is disabled or errors occur."""

    def __init__(self, name: str = "noop") -> None:
        self.name = name

    def end(self, *args: Any, **kwargs: Any) -> NoOpSpan:
        return self

    def update(self, *args: Any, **kwargs: Any) -> NoOpSpan:
        return self

    def span(self, *args: Any, **kwargs: Any) -> NoOpSpan:
        return self

    def generation(self, *args: Any, **kwargs: Any) -> NoOpSpan:
        return self

    def score(self, *args: Any, **kwargs: Any) -> NoOpSpan:
        return self


class TelemetryManager:
    """Manages Langfuse tracing for Triage Agent execution."""

    def __init__(self) -> None:
        self._client: Any | None = None
        self._initialized = False

    def is_enabled(self) -> bool:
        env = os.getenv("ENVIRONMENT", "").lower()
        if env == "test":
            return False
        pub = os.getenv("LANGFUSE_PUBLIC_KEY", "").strip()
        sec = os.getenv("LANGFUSE_SECRET_KEY", "").strip()
        return bool(pub and sec)

    def _get_client(self) -> Any | None:
        if not self.is_enabled():
            return None
        if not self._initialized:
            try:
                from langfuse import Langfuse

                self._client = Langfuse(
                    public_key=os.getenv("LANGFUSE_PUBLIC_KEY", "").strip(),
                    secret_key=os.getenv("LANGFUSE_SECRET_KEY", "").strip(),
                    host=os.getenv("LANGFUSE_HOST", "https://cloud.langfuse.com").strip().rstrip("/"),
                )
                self._initialized = True
            except Exception as exc:
                logger.warning("Failed to initialize Langfuse client: %s", exc)
                self._client = None
                self._initialized = True
        return self._client

    def start_trace(
        self,
        name: str = "incident-triage",
        session_id: str | None = None,
        user_id: str | None = None,
        input_data: Any | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Any:
        client = self._get_client()
        if client is None:
            return NoOpSpan(name)
        try:
            return client.trace(
                name=name,
                session_id=session_id,
                user_id=user_id,
                input=input_data,
                tags=tags or ["triage-agent"],
                metadata=metadata or {},
            )
        except Exception as exc:
            logger.warning("Failed to start Langfuse trace: %s", exc)
            return NoOpSpan(name)

    def end_trace(self, trace: Any, output_data: Any | None = None) -> None:
        if isinstance(trace, NoOpSpan) or trace is None:
            return
        try:
            if output_data is not None:
                trace.update(output=output_data)
        except Exception as exc:
            logger.warning("Failed to update Langfuse trace: %s", exc)

    def start_span(
        self,
        parent: Any,
        name: str,
        input_data: Any | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Any:
        if isinstance(parent, NoOpSpan) or parent is None:
            return NoOpSpan(name)
        try:
            return parent.span(name=name, input=input_data, metadata=metadata or {})
        except Exception as exc:
            logger.warning("Failed to start Langfuse span: %s", exc)
            return NoOpSpan(name)

    def end_span(self, span: Any, output_data: Any | None = None) -> None:
        if isinstance(span, NoOpSpan) or span is None:
            return
        try:
            if output_data is not None:
                span.end(output=output_data)
            else:
                span.end()
        except Exception as exc:
            logger.warning("Failed to end Langfuse span: %s", exc)

    def record_generation(
        self,
        parent: Any,
        name: str,
        model: str | None = None,
        input_data: Any | None = None,
        output_data: Any | None = None,
        usage: dict[str, int] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Any:
        if isinstance(parent, NoOpSpan) or parent is None:
            return NoOpSpan(name)
        try:
            kwargs: dict[str, Any] = {
                "name": name,
                "model": model,
                "input": input_data,
                "output": output_data,
                "metadata": metadata or {},
            }
            if usage:
                kwargs["usage"] = usage
            return parent.generation(**kwargs)
        except Exception as exc:
            logger.warning("Failed to record Langfuse generation: %s", exc)
            return NoOpSpan(name)

    def flush(self) -> None:
        client = self._client
        if client is not None:
            try:
                client.flush()
            except Exception as exc:
                logger.warning("Failed to flush Langfuse client: %s", exc)


telemetry = TelemetryManager()
