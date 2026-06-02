# Workspaces

A **Workspace** in Holzi is a directory on disk that the **Sandbox**
can read from. Workspaces are configured at boot time by the operator
and surfaced in two places:

- The **Workspace** tab in the right-hand rail of the Web-Chat
  (read-only file tree).
- The working directory for scheduled **Tasks**.

## What users can do

- Browse — expand directories, see files.
- Read a file — open any text file shown in the tree.

Writing to workspace files goes through the Sandbox approval flow and
isn't directly user-controllable from the UI; ask Hermes to make
changes via a tool call instead.

## How to reach the Workspace browser

Open the Web-Chat, expand the right rail, switch to the **Workspace**
tab. Each root that was configured at boot shows up here; click into
folders to expand.

## See also

- `memory` — for notes that persist independent of any workspace.
- `tasks` — scheduled runs use Workspaces as their working directory.
