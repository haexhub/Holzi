# Memory

Holzi gives Hermes two memory surfaces beyond the current chat.

## Notes

Persistent key/value markdown notes Hermes can write and search:

- `save_note(key, content, tags?)` — upsert a note.
- `get_note(key)` — fetch a specific note.
- `find_notes(query, tags?)` — full-text search across notes.

Notes are surfaced to users at **`/settings/memory`** as a two-pane
note browser.

## Conversation recall

Past conversations across all channels are searchable:

- `recall_memory(query)` — FTS search over messages and notes.
- `list_conversations()` / `get_conversation(id)` — browse history.

This means Hermes can answer "what did we decide last week about X"
even if the previous chat happened on Signal and the current one is
on Web.

## See also

- `workspaces` — for files on disk (orthogonal to notes).
- `tasks` — scheduled runs can read and write notes too.
