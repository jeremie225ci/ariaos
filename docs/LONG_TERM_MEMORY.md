# AriaOS Long-Term Memory

This document describes the long-term memory system that ships in the public
local-first AriaOS build.

## What Exists Today

AriaOS keeps long-term memory in two local layers:

- a human-readable Markdown file on the VM desktop
- a local SQLite embedding index used for semantic recall

Nothing in this flow depends on a hosted Aria server.

## Where The Data Lives

- `~/Desktop/Aria Memory.md`
  The readable source of truth for long-term memory.

- `~/.ariaos/data/aria_memory_index.db`
  The local SQLite index used for semantic retrieval of task summaries.

## Memory File Structure

The Markdown file is organized into four sections:

- `User Preferences`
  Stable personal preferences and durable user facts.

- `Verified Playbooks`
  Short, reusable fixes or procedures that were verified during previous work.

- `Recent Useful Task Summaries`
  Recent completed-task outcomes that may help with future requests.

- `Archive Summary`
  Older summaries compacted down when the memory file grows.

The file is intentionally readable so users can inspect and edit it directly.

## Write Path

When a task finishes, AriaOS uses a separate memory-decision pass to decide what
should be saved.

Main code paths:

- `app/aria-shell-gtk/src/aria_shell_gtk/services/prompts.py`
  Prompt that decides the memory mutation.

- `app/aria-shell-gtk/src/aria_shell_gtk/services/core.py`
  `RuntimeStore.decide_long_term_memory_action`
  Builds the decision request from the current goal, the final answer, a short
  transcript excerpt, and the current memory file.

- `app/aria-shell-gtk/src/aria_shell_gtk/services/core.py`
  `RuntimeStore.apply_long_term_memory_action`
  Applies the selected mutation to the Markdown memory file.

The write flow is:

1. Aria finishes a task.
2. A separate memory prompt decides whether the outcome is:
   - `user_preference`
   - `verified_playbook`
   - `useful_task_summary`
   - `remove_memory_item`
   - `update_memory_item`
   - `compact_summaries`
   - `none`
3. The Markdown memory file is updated.
4. The file is compacted and de-duplicated.
5. The embedding index is synchronized with the current summaries.

## Fallback Without An API Key

AriaOS still writes a limited amount of useful memory even without an OpenAI
API key.

In that fallback path, it can still:

- extract durable user preference lines from the prompt/transcript
- append useful task summaries for completed work

That behavior lives in:

- `app/aria-shell-gtk/src/aria_shell_gtk/services/core.py`
  `RuntimeStore.decide_long_term_memory_action`

## Vector Database Design

The vector layer is local SQLite, not an external vector service.

Implementation:

- `app/aria-shell-gtk/src/aria_shell_gtk/services/core.py`
  `_memory_index_connect`

SQLite tables:

- `summary_embeddings`
  Stores one embedding JSON vector per summary line.

- `memory_index_meta`
  Stores the current summaries hash and embedding model metadata.

Embedding model:

- `text-embedding-3-large`

Only summary-like sections are embedded:

- `Recent Useful Task Summaries`
- `Archive Summary`

Preferences and playbooks remain plain-text context because they are already
small, durable, and high-signal.

## Retrieval Path

When Aria needs memory context for a new task, it builds a compact snapshot from
the Markdown memory file plus the semantic index.

Main code paths:

- `app/aria-shell-gtk/src/aria_shell_gtk/services/core.py`
  `RuntimeStore.long_term_memory_snapshot`

- `app/aria-shell-gtk/src/aria_shell_gtk/services/core.py`
  `_extract_memory_context`

- `app/aria-shell-gtk/src/aria_shell_gtk/services/core.py`
  `_retrieve_semantic_memory_summaries`

The retrieval flow is:

1. Always include `User Preferences`.
2. Include `Verified Playbooks`.
3. Retrieve the most relevant summaries from the local embedding index.
4. Apply lexical overlap and section bias to stabilize ranking.
5. Fall back to keyword matching if embeddings are unavailable.
6. Trim the final memory block to the prompt token budget.

## Compaction Rules

AriaOS keeps the memory file bounded.

Main rules:

- recent summaries are capped
- archive lines are capped
- duplicate lines are removed
- obviously failed or low-value summaries are filtered out

If enough summaries accumulate, Aria can also ask a model to compact older
summaries into short archive lines.

Code path:

- `app/aria-shell-gtk/src/aria_shell_gtk/services/core.py`
  `_llm_compact_recent_summaries`

## Manual Editing Guidance

If you want to modify memory by hand, edit:

- `~/Desktop/Aria Memory.md`

Guidelines:

- keep entries short
- keep entries factual
- avoid duplicating the same summary with different wording
- put stable preferences in `User Preferences`
- put reusable fixes in `Verified Playbooks`

The next retrieval or memory update will re-sync the local embedding index from
that file.
