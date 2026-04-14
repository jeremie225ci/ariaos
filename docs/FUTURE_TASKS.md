# AriaOS Future Tasks

This document describes the current state of future-task support in the public
AriaOS repository.

## Current Status

The planner contract for future tasks already exists in the public brain.

What is already implemented:

- the system prompt teaches the model to classify future-task requests
- the brain can normalize and validate scheduling payloads
- the brain can normalize cancel, update, and replace mutations
- scheduled-task context can be injected into the planner prompt

What is not yet fully wired in the public GTK shell:

- persistent local storage of scheduled tasks
- passing active scheduled tasks back into the brain on later turns
- automatic local execution of due tasks

So today, the public build has a real future-task brain contract, but not yet a
complete local scheduler product path.

## Planner Rules

Future-task behavior starts in the prompts:

- `backend/prompts.py`
- `backend/brain_config.py`

The planner is explicitly told:

- classify requests that refer to later execution as future tasks
- do not execute browser or computer actions immediately for those requests
- ask for clarification if the execution time is missing
- return `schedule_task` when the request is clear enough
- use task IDs exactly for cancel, update, or replace operations

## Temporal Context

The brain injects a dedicated time block before planning:

- `backend/agentic_runtime.py`
  `_temporal_context_text`

That block gives the model:

- current local datetime
- current date
- current timezone
- the rule that future tasks must be converted to exact ISO-8601 datetimes

## Scheduled Tasks Context

The brain also supports an injected scheduled-task context:

- `backend/agentic_runtime.py`
  `configure_scheduler_context`

- `backend/agentic_runtime.py`
  `_scheduled_tasks_context_text`

That context is what lets the model:

- list known future tasks
- target the correct `task_id`
- update or replace the correct task

In the current public shell path, this context is still empty:

- `backend/loopagentic.py`
  `brain.configure_scheduler_context([])`

So the prompt contract is present, but the shell is not yet feeding persisted
scheduled tasks back into the brain.

## Normalized Scheduling Payloads

The runtime validates scheduling requests before they leave the brain as task
results.

Main code paths:

- `backend/agentic_runtime.py`
  `_normalize_schedule_action`

- `backend/agentic_runtime.py`
  `_normalize_scheduled_task_mutation`

- `backend/agentic_runtime.py`
  `_normalize_replace_scheduled_task_action`

The normalization layer enforces:

- non-empty goal/prompt
- exact ISO-8601 `run_at`
- future-only schedule times
- normalized timezone
- limited recurrence support

Supported recurrence kinds today:

- `daily`
- `weekly`
- `monthly`

## Brain Result Shape

When the planner decides a future-task action, the agentic runtime returns a
structured payload internally:

- `deferredTask` for `schedule_task`
- `scheduledTaskMutation` for cancel/update/replace operations

Those payloads are produced in:

- `backend/agentic_runtime.py`

## Why The Public Shell Is Not End-To-End Yet

The local websocket path still only returns:

- streamed text/log chunks
- final `response`
- `usage`

Current code paths:

- `backend/server.py`
- `app/aria-shell-gtk/src/aria_shell_gtk/services/loop.py`

That means the GTK shell does not yet receive or persist:

- `deferredTask`
- `scheduledTaskMutation`

So the public build can already think about future tasks correctly, but the
last-mile local scheduler wiring remains to be added.

## What A Full Local Scheduler Needs Next

To complete the feature locally, AriaOS needs to add:

1. a local task store for scheduled tasks
2. shell/backend transport for `deferredTask` and mutation payloads
3. reinjection of active tasks through `configure_scheduler_context`
4. a local runner that executes due tasks through the same visible brain

That is the remaining gap between the current planner contract and a complete
local future-task system.
