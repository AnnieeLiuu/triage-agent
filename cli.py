"""Interactive CLI for PickForMe Incident Triage Agent."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from dotenv import load_dotenv
from src.harness import GeminiClient, TrajectoryEvent, TriageHarness
from src.mcp_client import MCPStdioClient
from src.skills_loader import SkillLoader


def _print_event(event: TrajectoryEvent) -> None:
    if event.event_type == "skill_loaded":
        print(f"  [Step {event.step}] [Skill] 載入 `{event.detail['skill']}`")
    elif event.event_type == "tool_call":
        perm = event.detail["permission"]
        args_str = json.dumps(event.detail["arguments"], ensure_ascii=False)
        print(f"  [Step {event.step}] [Tool:{perm}] {event.detail['tool']}({args_str})")
    elif event.event_type == "permission_decision":
        status = "核准" if event.detail["approved"] else "拒絕"
        print(f"  [Step {event.step}] [Gate] 人類確認結果：{status}")
    elif event.event_type == "circuit_breaker":
        print(f"  [Step {event.step}] [CircuitBreaker] 觸發步數熔斷")


def _prompt_human_confirmation(tool_name: str, arguments: dict[str, Any]) -> bool:
    print("\n--- Human-in-the-Loop WRITE 確認 ---")
    print(f"工具名稱: {tool_name}")
    print(f"寫入參數: {json.dumps(arguments, ensure_ascii=False, indent=2)}")
    answer = input("是否允許執行此寫入操作？ [y/N]: ").strip().lower()
    return answer in {"y", "yes"}


def main(argv: list[str] | None = None) -> int:
    load_dotenv()

    parser = argparse.ArgumentParser(description="PickForMe Incident Triage Agent CLI")
    parser.add_argument("query", nargs="?", help="要排查的問題描述（省略則進入互動模式）")
    parser.add_argument("--max-steps", type=int, default=8, help="最大 ReAct 步數（預設 8）")
    args = parser.parse_args(argv)

    try:
        llm = GeminiClient()
    except ValueError as exc:
        print(f"設定錯誤: {exc}", file=sys.stderr)
        print("請先複製 .env.example 為 .env 並填入 GEMINI_API_KEY。", file=sys.stderr)
        return 1

    with MCPStdioClient() as mcp_client:
        mcp_tools = mcp_client.build_tool_specs()
        harness = TriageHarness(
            llm_client=llm,
            skill_loader=SkillLoader("skills"),
            tools=mcp_tools,
            max_steps=args.max_steps,
            confirm_fn=_prompt_human_confirmation,
            on_event=_print_event,
        )

        if args.query:
            result = harness.run(args.query)
            print(f"\n=== 排查結論 ({result.stopped_reason}, {result.steps_used} steps) ===")
            print(result.answer)
            return 0

        print("PickForMe Incident Triage Agent (輸入 'exit' 或 'quit' 離開)")
        history: list[dict[str, Any]] = []
        while True:
            try:
                query = input("\n請輸入排查需求> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not query or query.lower() in {"exit", "quit"}:
                break
            result = harness.run(query, history=history)
            print(f"\n=== 排查結論 ({result.stopped_reason}, {result.steps_used} steps) ===")
            print(result.answer)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
