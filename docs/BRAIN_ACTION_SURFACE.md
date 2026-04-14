# AriaOS Brain Action Surface

This document explains what the public AriaOS brain can actually do today, how the planner is wired, and which actions are meant for desktop work, browser work, file work, and programming work.

## Overview

The local AriaOS runtime has two execution layers:

1. The top-level planner loop builds a prompt, reads session context, and returns exactly one JSON action per step.
2. The `computer` action launches a second loop that uses the OpenAI native computer-use tool for real on-screen interaction.

The key files are:

- `backend/brain_config.py`
  The visible system prompt and planner rules.
- `backend/agentic_runtime.py`
  The runtime that parses planner JSON actions and executes them locally.
- `backend/loopagentic.py`
  Thin bridge from the websocket shell into the runtime.
- `backend/server.py`
  Local websocket server used by Terminal Aria.

## Planner Contract

The main planner does not receive Python callables for most tools. It is instructed through the system prompt to emit one JSON object per step.

Current contract shape:

```json
{
  "thought": "why Aria is choosing the next step",
  "progress_log": "short validated progress summary",
  "action": "one action name",
  "...": "action-specific fields"
}
```

Important planner rules enforced by the prompt:

- `computer` is the only UI action for visible browser, desktop, mouse, keyboard, and visual verification work.
- legacy UI actions such as `click`, `type_text`, `scroll`, `read_screen`, and `ask_vision` are not allowed at planner level
- non-visual helper work can use specialized local tools instead of opening Terminal
- programming/file creation should prefer `write_file`
- browser/Gmail/form/upload flows should stay in specialized helpers plus `computer`, not Terminal
- future tasks should return scheduling actions instead of executing immediately

## Action Families

### Core task actions

These actions define the high-level planner flow:

- `respond`
- `reply`
- `done`
- `failed`
- `schedule_task`
- `cancel_scheduled_task`
- `delete_scheduled_task`
- `update_scheduled_task`
- `edit_scheduled_task`
- `reschedule_scheduled_task`
- `replace_scheduled_task`
- `swap_scheduled_task`

### Non-visual shell and control actions

These are for helper work that does not require visible UI interaction:

- `execute`
- `wait`
- `python_exec`

`execute` is intentionally blocked for browser/navigation flows when it tries to replace visual interaction with Terminal automation.

### UI and desktop actions

The planner-visible desktop path is:

- `computer`

Before `computer`, the planner can use local navigation helpers when they fit:

- `open_url`
- `open_file`
- `reveal_path`
- `open_app`
- `focus_window`
- `list_windows`
- `list_processes`
- `read_clipboard`
- `read_clipboard_image`
- `copy_to_clipboard`
- `paste`

These helpers are meant for:

- opening the right app or file
- focusing the right window
- checking which windows/processes are already present
- resolving clipboard state

They are not meant to replace visual interaction inside a live window. Once the task requires clicking, typing in a visible field, scrolling, dragging, or screen verification, Aria must use `computer`.

### File and coding actions

These are the main programming and local file-management actions currently implemented in the runtime:

- `write_file`
- `read_file`
- `list_dir`
- `append_file`
- `replace_in_file`
- `replace_regex_in_file`
- `replace_block_in_file`
- `tail_file`
- `read_path`
- `search_files`
- `grep_in_files`
- `grep_ast`
- `stat_path`
- `diff_paths`
- `make_dir`
- `move_path`
- `copy_path`
- `delete_path`
- `chmod_path`
- `symlink_path`
- `unzip_path`
- `archive_path`
- `read_json`
- `write_json`
- `read_json_schema`
- `read_yaml`
- `write_yaml`
- `read_yaml_schema`
- `read_csv_preview`
- `git_status`
- `git_diff`
- `run_tests`

For coding tasks, the intended pattern is usually:

1. inspect with `read_file`, `read_path`, `search_files`, `grep_in_files`, or `grep_ast`
2. create or update with `write_file`, `append_file`, `replace_in_file`, `replace_regex_in_file`, or `replace_block_in_file`
3. validate with `run_tests`, `git_diff`, `git_status`, or `execute`

### System and local machine actions

These are available for deeper local machine work:

- `list_ports`
- `process_tree`
- `kill_process`
- `disk_usage`
- `find_large_files`
- `env_get`
- `env_set_local`
- `http_request`
- `sqlite_query`
- `docker_ps`
- `docker_logs`
- `systemctl_status`
- `systemctl_restart`
- `download_file`
- `extract_text_from_pdf`

### Browser/upload helpers

These helpers exist so Aria does not need to brute-force file selection through Terminal:

- `select_file_for_active_dialog`
- `upload_file_to_active_app`
- `drag_file_to_target`
- `list_downloads`
- `watch_file`
- `watch_dir`

### Creative and media actions

These actions are also implemented locally:

- `ffmpeg_run`
- `blender_render`
- `compose_video_from_images`
- `generate_image`
- `generate_image_ai`
- `upscale_image_ai`
- `remove_background_ai`
- `edit_image_ai`
- `extract_video_frames`
- `storyboard_to_video`
- `ocr_region`

## Computer Subloop

When the planner returns:

```json
{"thought":"...","progress_log":"...","action":"computer","instruction":"..."}
```

the runtime starts a dedicated OpenAI Responses computer-use loop.

That subloop currently executes these low-level actions:

- `click`
- `double_click`
- `scroll`
- `type`
- `keypress`
- `key_press`
- `wait`
- `move`
- `drag`
- `screenshot`

This means the public AriaOS architecture is:

- planner chooses the next semantic step
- `computer` handles low-level visible interaction
- specialized helpers handle non-visual local work

## Guard Rails

Important runtime restrictions:

- planner-level legacy UI actions are blocked and must be replaced by `computer`
- explicit URLs in a `computer` instruction are rerouted to `open_url` first
- explicit file references in a `computer` instruction are rerouted to `open_file`, `reveal_path`, `search_files`, or upload helpers first
- browser, Gmail, form, and upload flows must not use Terminal as a visible inspection surface
- `execute` is blocked when it tries to replace browser/computer interaction
- blind restarts are blocked when the current task already has validated state

## Current Model Defaults

The public runtime defaults to:

- planner model: `gpt-5.4`
- supported local planner models: `gpt-5.4`, `gpt-5.4-mini`

The prompt and the runtime should stay aligned on that point.

## Where To Read The Code

If you want the exact implementation, start here:

- planner prompt and allowed action contract: `backend/brain_config.py`
- main planner loop: `backend/agentic_runtime.py`, `execute_goal(...)`
- native computer-use loop: `backend/agentic_runtime.py`, `_run_computer_use(...)`
- low-level computer actions: `backend/agentic_runtime.py`, `_execute_computer_action(...)`
- helper-vs-computer routing rules: `backend/agentic_runtime.py`, `_should_use_specialized_tool_before_computer(...)`
- shell-blocking rules for browser tasks: `backend/agentic_runtime.py`, `_should_block_execute(...)`

This file is meant to describe the public behavior exposed by the current repo, not the old hidden client/server architecture.
