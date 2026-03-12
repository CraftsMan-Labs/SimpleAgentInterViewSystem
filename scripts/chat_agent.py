from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from simple_agents_py import Client


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run SimpleAgents chat interview workflow"
    )
    parser.add_argument(
        "--workflow",
        default="workflows/python-intern-fun-interview-chat.yaml",
        help="Path to workflow YAML",
    )
    parser.add_argument(
        "--max-turns",
        type=int,
        default=12,
        help="Maximum turns before automatic exit",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Optional model override for all llm_call nodes",
    )
    return parser.parse_args()


def load_config() -> tuple[str, str, str]:
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    load_dotenv()

    provider = os.getenv("WORKFLOW_PROVIDER", "openai").strip()
    api_base = os.getenv("WORKFLOW_API_BASE", "").strip()
    api_key = os.getenv("WORKFLOW_API_KEY", "").strip()

    if api_base == "" or api_key == "":
        raise RuntimeError(
            "Set WORKFLOW_API_BASE and WORKFLOW_API_KEY in .env before running."
        )

    return provider, api_base, api_key


def resolve_workflow_path(raw_path: str) -> Path:
    candidate = Path(raw_path)
    if candidate.exists():
        return candidate

    local = Path(__file__).resolve().parents[1] / raw_path
    if local.exists():
        return local

    raise FileNotFoundError(f"Workflow file not found: {raw_path}")


def render_reply(terminal_output: Any) -> str:
    if isinstance(terminal_output, str):
        return terminal_output
    if not isinstance(terminal_output, dict):
        return json.dumps(terminal_output, ensure_ascii=True)

    for key in ("message", "question", "feedback", "reason"):
        value = terminal_output.get(key)
        if isinstance(value, str) and value.strip() != "":
            return value.strip()

    return json.dumps(terminal_output, ensure_ascii=True)


def is_closed_session(terminal_node: Any, terminal_output: Any) -> bool:
    if isinstance(terminal_node, str) and terminal_node in {
        "terminate_candidate",
        "already_terminated",
    }:
        return True

    if isinstance(terminal_output, dict):
        decision = terminal_output.get("decision")
        if isinstance(decision, str) and decision.strip().lower() == "terminated":
            return True

    return False


def main() -> None:
    args = parse_args()
    provider, api_base, api_key = load_config()
    workflow_path = resolve_workflow_path(args.workflow)

    configured_model = os.getenv("WORKFLOW_MODEL", "").strip()
    model_override = (
        args.model.strip()
        if isinstance(args.model, str) and args.model.strip() != ""
        else configured_model
    )

    client = Client(provider, api_base=api_base, api_key=api_key)
    messages: list[dict[str, str]] = [
        {
            "role": "system",
            "content": (
                "You are an interview assistant. Keep it structured, fair, and ask one question at a time."
            ),
        }
    ]

    print("SimpleAgents Interview Chat")
    print("Type 'exit' to quit.\n")

    for _ in range(args.max_turns):
        user_text = input("You: ").strip()
        if user_text == "":
            continue
        if user_text.lower() in {"exit", "quit"}:
            print("Bye!")
            return

        messages.append({"role": "user", "content": user_text})
        workflow_input: dict[str, Any] = {"messages": messages}

        workflow_options: dict[str, Any] = {}
        if model_override != "":
            workflow_options["model"] = model_override

        result = client.run_workflow_yaml(
            str(workflow_path),
            workflow_input,
            include_events=False,
            workflow_options=workflow_options,
        )

        terminal_output = (
            result.get("terminal_output") if isinstance(result, dict) else None
        )
        terminal_node = (
            result.get("terminal_node") if isinstance(result, dict) else None
        )
        assistant_reply = render_reply(terminal_output)

        print(f"\nAssistant: {assistant_reply}\n")
        messages.append({"role": "assistant", "content": assistant_reply})

        if is_closed_session(terminal_node, terminal_output):
            print("Interview closed. Start a new run for a new candidate.")
            return

    print("Reached max turns. Restart to continue.")


if __name__ == "__main__":
    main()
