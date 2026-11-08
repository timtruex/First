"""Script writer backed by the Claude Agent SDK.

Signed in with a Claude plan, the Agent SDK draws on the subscription's limits rather
than pay-as-you-go API credits, which is the only reason this backend exists: it is a
billing route, not a better writer. The job is still one prompt in, one JSON object out.

Two things matter for that to hold. ANTHROPIC_API_KEY must not be set, or the run bills
API credits while appearing to use the plan. And the agent must stay inert: no tools, no
settings files, one turn. It is writing copy, not operating a computer.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import Any

from ..config import Config
from ..models import ScriptDraft
from .prompts import SYSTEM, build_user_prompt

log = logging.getLogger(__name__)

# Bare names remove a tool from the model's context entirely.
NO_TOOLS = [
    "Bash", "Read", "Write", "Edit", "NotebookEdit", "Glob", "Grep",
    "WebSearch", "WebFetch", "Task", "TodoWrite",
]

JSON_INSTRUCTION = """\

OUTPUT FORMAT
Reply with one JSON object and nothing else. No prose before or after it, no markdown
fence. These keys exactly:

{
  "scenes": [{"narration": str, "kind": str, "on_screen_text": str, "image_hint": str}],
  "pros": [str, str, str],
  "cons": [str],
  "verdict": str,
  "youtube_title": str,
  "youtube_description": str,
  "tags": [str],
  "shorts": [{"title": str, "scene_indices": [int]}],
  "thumbnail_text": str
}

"kind" is one of: hook, feature, spec, con, verdict, cta.
"""


class AgentScriptError(RuntimeError):
    pass


def write_script_with_agent(cfg: Config, product) -> ScriptDraft:
    """Run the Agent SDK once and parse its reply into a ScriptDraft."""
    try:
        from claude_agent_sdk import CLINotFoundError  # noqa: F401
    except ImportError as exc:
        raise AgentScriptError(
            "the Claude Agent SDK is not installed. Run:\n"
            '    ./.venv/bin/pip install -e ".[agent]"'
        ) from exc

    if os.environ.get("ANTHROPIC_API_KEY"):
        # Not fatal, but the operator chose this backend to avoid API billing.
        log.warning(
            "ANTHROPIC_API_KEY is set, so this run may bill API credits rather than "
            "your Claude plan. Remove it from the environment and from [script] "
            "api_key in config.toml to use the subscription."
        )

    prompt = build_user_prompt(cfg, product) + JSON_INSTRUCTION
    raw = asyncio.run(_ask(cfg, prompt))
    return _parse(raw)


async def _ask(cfg: Config, prompt: str) -> str:
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        CLINotFoundError,
        ResultMessage,
        TextBlock,
        query,
    )

    options = ClaudeAgentOptions(
        system_prompt=SYSTEM,
        model=cfg.script.model or None,
        max_turns=1,
        setting_sources=[],      # ignore user, project and local settings files
        disallowed_tools=NO_TOOLS,
        permission_mode="dontAsk",
    )

    chunks: list[str] = []
    result_text = ""
    failure = ""

    try:
        # Consume the whole stream: the SDK warns that breaking out early upsets
        # asyncio cleanup.
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        chunks.append(block.text)
            elif isinstance(message, ResultMessage):
                if getattr(message, "is_error", False):
                    failure = f"{getattr(message, 'subtype', 'error')}"
                result_text = getattr(message, "result", "") or ""
    except CLINotFoundError as exc:
        raise AgentScriptError(
            "the Claude Code binary that the Agent SDK drives could not be found. "
            "Install Claude Code, or switch back with backend = \"api\" in config.toml."
        ) from exc
    except Exception as exc:  # the SDK surfaces auth and transport problems here
        raise AgentScriptError(
            f"the Agent SDK call failed: {exc}\n"
            "If this is an authentication error, sign in to Claude Code on this machine "
            "so the SDK can use your plan."
        ) from exc

    if failure:
        raise AgentScriptError(f"the Agent SDK ended with {failure}")

    text = result_text or "".join(chunks)
    if not text.strip():
        raise AgentScriptError("the Agent SDK returned no text")
    return text


def _parse(text: str) -> ScriptDraft:
    """Pull the JSON object out of the reply, tolerating a fence or a stray sentence."""
    candidate = text.strip()

    fenced = re.search(r"```(?:json)?\s*(.+?)\s*```", candidate, re.S)
    if fenced:
        candidate = fenced.group(1).strip()

    if not candidate.startswith("{"):
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start == -1 or end <= start:
            raise AgentScriptError(f"no JSON object in the reply: {candidate[:200]}")
        candidate = candidate[start : end + 1]

    try:
        data: Any = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise AgentScriptError(f"the reply was not valid JSON ({exc.msg})") from exc

    try:
        return ScriptDraft.model_validate(data)
    except Exception as exc:
        raise AgentScriptError(f"the reply did not match the script shape: {exc}") from exc
