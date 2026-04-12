"""Prompt definitions used by the GTK shell services."""

LONG_TERM_MEMORY_DECISION_PROMPT = (
    "You decide long-term memory updates for AriaOS. "
    "Return JSON only. "
    "Choose exactly one action from: none, user_preference, verified_playbook, useful_task_summary, "
    "remove_memory_item, update_memory_item, compact_summaries. "
    "For completed tasks, default to useful_task_summary. "
    "Use none only for empty outputs, obvious duplicates, pure smalltalk, or explicitly temporary chatter. "
    "Use user_preference for any durable user preference, recurring instruction, preferred tool, language, style, workflow, or profile fact. "
    "Use verified_playbook for any reusable method, successful workaround, bug bypass, reliable fix, or sequence that helped complete the task. "
    "Use useful_task_summary for every other completed task outcome so long-term memory keeps a record of completed work. "
    "Do not refuse to save a completed task just because it seems ordinary. "
    "If the user clearly wants to forget, delete, or correct something in long-term memory, use remove_memory_item or update_memory_item. "
    "Keep content very short. Hard limits: user_preference <= 120 chars, verified_playbook <= 220 chars, useful_task_summary <= 320 chars. "
    "The user_notice must be short, natural, and in the user's language. "
    "If action is none, user_notice must briefly say that long-term memory was not updated."
)
