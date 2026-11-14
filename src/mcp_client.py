"""Real MCP stdio client connecting TriageHarness to the standalone FastMCP ops_server process."""

from __future__ import annotations

import asyncio
import json
import sys
import threading
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from src.harness import ToolPermission, ToolSpec


class MCPStdioClient:
    """Manages a subprocess FastMCP server over stdio and exposes its tools as ToolSpecs."""

    def __init__(
        self,
        command: str | None = None,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
    ) -> None:
        self._server_params = StdioServerParameters(
            command=command or sys.executable,
            args=args if args is not None else ["-m", "src.mcp_server.ops_server"],
            env=env,
            cwd=cwd,
        )
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._stop_event: asyncio.Event | None = None
        self._session: ClientSession | None = None

    def __enter__(self) -> MCPStdioClient:
        self._loop = asyncio.new_event_loop()
        ready = threading.Event()
        startup_error: list[BaseException] = []

        async def _lifecycle() -> None:
            assert self._loop is not None
            self._stop_event = asyncio.Event()
            try:
                async with stdio_client(self._server_params) as (read_stream, write_stream):
                    async with ClientSession(read_stream, write_stream) as session:
                        await session.initialize()
                        self._session = session
                        ready.set()
                        await self._stop_event.wait()
            except BaseException as exc:  # pragma: no cover - surfaced below
                if not ready.is_set():
                    startup_error.append(exc)
                    ready.set()
                else:
                    raise
            finally:
                self._session = None

        def _runner() -> None:
            assert self._loop is not None
            asyncio.set_event_loop(self._loop)
            self._loop.run_until_complete(_lifecycle())

        self._thread = threading.Thread(target=_runner, daemon=True, name="mcp-stdio-loop")
        self._thread.start()
        ready.wait()

        if startup_error:
            self._cleanup()
            raise startup_error[0]
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if self._loop is not None and self._stop_event is not None and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._stop_event.set)
        if self._thread is not None:
            self._thread.join(timeout=10)
        self._cleanup()

    def _cleanup(self) -> None:
        if self._loop is not None and not self._loop.is_closed():
            self._loop.close()
        self._loop = None
        self._thread = None
        self._stop_event = None
        self._session = None

    def list_tools(self) -> list[Any]:
        """Call MCP list_tools() on the connected FastMCP server."""
        if self._session is None or self._loop is None:
            raise RuntimeError("MCPStdioClient is not connected.")
        fut = asyncio.run_coroutine_threadsafe(self._session.list_tools(), self._loop)
        result = fut.result(timeout=15)
        return list(result.tools)

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        """Call an MCP tool via session.call_tool() over stdio transport."""
        if self._session is None or self._loop is None:
            raise RuntimeError("MCPStdioClient is not connected.")

        async def _invoke() -> Any:
            assert self._session is not None
            res = await self._session.call_tool(name, arguments=arguments or {})
            return self._extract_result(res)

        fut = asyncio.run_coroutine_threadsafe(_invoke(), self._loop)
        return fut.result(timeout=30)

    def build_tool_specs(self) -> dict[str, ToolSpec]:
        """Discover tools from the MCP server and wrap each as a Harness ToolSpec."""
        specs: dict[str, ToolSpec] = {}
        for tool in self.list_tools():
            annotations = getattr(tool, "annotations", None)
            read_only = getattr(annotations, "readOnlyHint", None) is True
            # Fail-closed: only tools explicitly annotated readOnlyHint=True are READ.
            permission = ToolPermission.READ if read_only else ToolPermission.WRITE
            tool_name = tool.name

            def _make_caller(name: str) -> Any:
                def _call(**kwargs: Any) -> Any:
                    return self.call_tool(name, kwargs)

                return _call

            specs[tool_name] = ToolSpec(
                name=tool_name,
                description=tool.description or "",
                permission=permission,
                fn=_make_caller(tool_name),
                parameters=dict(tool.inputSchema or {"type": "object", "properties": {}}),
            )
        return specs

    @staticmethod
    def _extract_result(call_result: Any) -> Any:
        structured = getattr(call_result, "structuredContent", None)
        if structured is not None:
            return structured.get("result", structured) if isinstance(structured, dict) else structured

        contents = getattr(call_result, "content", None) or []
        texts = [getattr(c, "text", None) for c in contents if getattr(c, "text", None) is not None]
        if len(texts) == 1:
            try:
                return json.loads(texts[0])
            except json.JSONDecodeError:
                return texts[0]
        if texts:
            parsed = []
            for t in texts:
                try:
                    parsed.append(json.loads(t))
                except json.JSONDecodeError:
                    parsed.append(t)
            return parsed
        return {"isError": getattr(call_result, "isError", False)}
