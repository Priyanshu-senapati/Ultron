"""Agent definitions: role, system prompt, tool allow-list."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class AgentRole(str, Enum):
    """Built-in agent roles. New roles can be registered at runtime via
    ``AgentMesh.register_agent``."""

    COORDINATOR = "coordinator"
    RESEARCHER = "researcher"
    CODER = "coder"
    REVIEWER = "reviewer"
    SYSADMIN = "sysadmin"


# Default system prompts. Kept terse — heavy persona work is C's job; here
# we focus on each agent's *job*, not personality.
_COORDINATOR_PROMPT = """\
You are the COORDINATOR agent. The user has asked for something that may
require multiple steps. Plan briefly, then call tools to make progress.
You may call: read_file, web_search, knowledge_search, memory_query,
screenshot. For anything that mutates state (shell, write_file,
delete_file), explain what you'd do and stop — the user will approve.

Respond with a tool call in a fenced ``tool`` block, or with your final
answer in plain prose. One tool per turn.\
"""

_RESEARCHER_PROMPT = """\
You are the RESEARCHER agent. Your only job is to gather information.
You may call: web_search, knowledge_search, memory_query, read_file.
You may NOT call shell, write_file, or delete_file. Summarise findings
in plain prose. One tool per turn.\
"""

_CODER_PROMPT = """\
You are the CODER agent. You can read files and propose edits, but you
will NOT execute writes without user approval. Available tools:
read_file, knowledge_search, web_search. When you have a final code
proposal, output it inline in a fenced code block and stop calling
tools.\
"""

_REVIEWER_PROMPT = """\
You are the REVIEWER agent. Read provided code or text and identify
issues — correctness, security, style. Tools: read_file,
knowledge_search. No writes, no shell. Return a structured review.\
"""

_SYSADMIN_PROMPT = """\
You are the SYSADMIN agent. You may run diagnostic shell commands and
check files. Tools: shell, read_file, memory_query. Every shell command
is confirm-required — you will get a pending state if you call shell;
explain why before issuing.\
"""


_ROLE_DEFAULTS: dict[AgentRole, tuple[str, tuple[str, ...]]] = {
    AgentRole.COORDINATOR: (
        _COORDINATOR_PROMPT,
        ("read_file", "web_search", "knowledge_search", "memory_query", "screenshot", "code_query", "money_query", "wellness_query", "plan_query", "kg_query", "dopamine_query"),
    ),
    AgentRole.RESEARCHER: (
        _RESEARCHER_PROMPT,
        ("web_search", "knowledge_search", "memory_query", "read_file", "code_query", "money_query", "wellness_query", "plan_query", "kg_query", "dopamine_query"),
    ),
    AgentRole.CODER: (
        _CODER_PROMPT,
        ("read_file", "knowledge_search", "web_search", "code_query"),
    ),
    AgentRole.REVIEWER: (
        _REVIEWER_PROMPT,
        ("read_file", "knowledge_search", "code_query"),
    ),
    AgentRole.SYSADMIN: (
        _SYSADMIN_PROMPT,
        ("shell", "read_file", "memory_query"),
    ),
}


@dataclass
class Agent:
    role: str
    system_prompt: str
    allowed_tools: tuple[str, ...]
    max_tool_rounds: int = 6

    @classmethod
    def for_role(cls, role: AgentRole, max_tool_rounds: int = 6) -> "Agent":
        prompt, tools = _ROLE_DEFAULTS[role]
        return cls(
            role=role.value,
            system_prompt=prompt,
            allowed_tools=tools,
            max_tool_rounds=max_tool_rounds,
        )
