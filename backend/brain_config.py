"""Local-first brain configuration for the public AriaOS runtime.

This mirrors the VM-era agentic configuration, but keeps the community build
independent from the old remote parser stack. The planner still gets the same
core prompt and execution limits while everything runs locally inside the VM.
"""

from __future__ import annotations

import os
from pathlib import Path

from backend.prompts import (
    AGENTIC_SYSTEM_PROMPT_TEMPLATE,
    DIRECT_RESPONSE_SYSTEM_PROMPT,
)

USER_HOME = os.path.expanduser("~")
MEMORY_DIR = os.path.join(USER_HOME, ".ariaos", "memory")
CHAT_HISTORY_FILE = os.path.join(MEMORY_DIR, "chat_history.json")
TASK_LOG_FILE = os.path.join(MEMORY_DIR, "task_log.json")
USER_PROFILE_FILE = os.path.join(MEMORY_DIR, "user_profile.json")
TASK_SUMMARIES_FILE = os.path.join(MEMORY_DIR, "task_summaries.json")
SESSION_LOG_DIR = os.path.join(MEMORY_DIR, "sessions")

OPENAI_API_KEY = str(os.environ.get("OPENAI_API_KEY") or "").strip()
OPENAI_BASE_URL = (
    str(os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1").strip()
    or "https://api.openai.com/v1"
)
OPENAI_MODEL = (
    str(os.environ.get("ARIA_OPENAI_MODEL") or os.environ.get("OPENAI_MODEL") or "gpt-5.4").strip()
    or "gpt-5.4"
)
OPENAI_TEMPERATURE = 0.1
OPENAI_MAX_TOKENS = 100000

MAX_HISTORY_MESSAGES = 50
CONTEXT_WINDOW_SIZE = 10
MAX_AGENTIC_STEPS = 0
COMMAND_TIMEOUT = 45
STEP_DELAY = 0.3

# The old visual parser stack is intentionally not part of the community build.
# The runtime falls back to screenshot + OpenAI vision/computer use instead.
PERCEPTION_LAYERS = ["vision"]
PERCEPTION_MAX_ELEMENTS = int(os.environ.get("ARIA_PERCEPTION_MAX_ELEMENTS", "200"))
PERCEPTION_MAX_CHARS = int(os.environ.get("ARIA_PERCEPTION_MAX_CHARS", "12000"))
VISUAL_PARSER_URL = str(os.environ.get("ARIA_VISUAL_PARSER_URL") or "").strip()
VISUAL_PARSER_TIMEOUT = int(os.environ.get("ARIA_VISUAL_PARSER_TIMEOUT", "12"))
VISUAL_PARSER_RETRY_AFTER = int(os.environ.get("ARIA_VISUAL_PARSER_RETRY_AFTER", "30"))

SNIPER_API_KEY = ""
SNIPER_BASE_URL = ""
SNIPER_MODEL = ""

SLIDING_WINDOW_RAW_MESSAGES = 8
PREFER_KERNEL_INPUT = (
    str(os.environ.get("ARIA_PREFER_KERNEL_INPUT") or "false").lower()
    in ("1", "true", "yes", "on")
)
SCREEN_RESOLUTION = (1280, 800)
ACTION_RETRY_LIMIT = int(os.environ.get("ARIA_ACTION_RETRY_LIMIT", "2"))
ACTION_SETTLE_DELAY = float(os.environ.get("ARIA_ACTION_SETTLE_DELAY", "0.25"))
TYPE_TEXT_DELAY = float(os.environ.get("ARIA_TYPE_TEXT_DELAY", "0.03"))

TRACE_ENABLED = (
    str(os.environ.get("ARIA_TRACE_ENABLED") or "true").lower()
    in ("1", "true", "yes", "on")
)
TRACE_FILE = str(os.environ.get("ARIA_TRACE_FILE") or "/tmp/ariaos_agent_trace.jsonl").strip()

# The copied VM runtime expects a literal "{memory_context}" placeholder that
# it can replace at execution time.
SYSTEM_PROMPT = AGENTIC_SYSTEM_PROMPT_TEMPLATE.replace(
    "__ARIA_MEMORY_CONTEXT__",
    "{memory_context}",
)

__all__ = [
    "ACTION_RETRY_LIMIT",
    "ACTION_SETTLE_DELAY",
    "CHAT_HISTORY_FILE",
    "COMMAND_TIMEOUT",
    "CONTEXT_WINDOW_SIZE",
    "DIRECT_RESPONSE_SYSTEM_PROMPT",
    "MAX_AGENTIC_STEPS",
    "MAX_HISTORY_MESSAGES",
    "MEMORY_DIR",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENAI_MAX_TOKENS",
    "OPENAI_MODEL",
    "OPENAI_TEMPERATURE",
    "PERCEPTION_LAYERS",
    "PERCEPTION_MAX_CHARS",
    "PERCEPTION_MAX_ELEMENTS",
    "PREFER_KERNEL_INPUT",
    "SCREEN_RESOLUTION",
    "SESSION_LOG_DIR",
    "SLIDING_WINDOW_RAW_MESSAGES",
    "SNIPER_API_KEY",
    "SNIPER_BASE_URL",
    "SNIPER_MODEL",
    "STEP_DELAY",
    "SYSTEM_PROMPT",
    "TASK_LOG_FILE",
    "TASK_SUMMARIES_FILE",
    "TRACE_ENABLED",
    "TRACE_FILE",
    "TYPE_TEXT_DELAY",
    "USER_PROFILE_FILE",
    "VISUAL_PARSER_RETRY_AFTER",
    "VISUAL_PARSER_TIMEOUT",
    "VISUAL_PARSER_URL",
]
