# Tasks

A **Task** is an agent run that executes without a user typing in
chat. Tasks are managed at **`/settings/tasks`**.

## Two kinds

- **One-shot** — runs once at a specific time (or immediately).
- **Recurring** — runs on a cron schedule (e.g. every weekday at
  08:00).

Each task carries a prompt: the instruction Hermes executes when the
task fires.

## Creating a task

1. Open `/settings/tasks`.
2. Trigger the create-task action (the primary "new" button at the
   top of the page).
3. Fill in title and prompt.
4. Pick one-shot (date/time) or recurring (cron expression).
5. Save.

Tasks run in the `task` channel, which has a tighter default prompt
(terse, result-focused) than Web-Chat.

## Tool surface

Hermes can manage tasks itself via:

- `task_create(title, prompt, due_at? | schedule?, timezone?)`
- `task_list()`
- `task_delete(id)`

When the user says "remind me tomorrow at 9", call `task_create` with
`due_at`. When they say "send me a weekly summary every Monday", use
`schedule` instead.

## See also

- `memory` — tasks can read and write notes for cross-run state.
