"""Pydantic request/response models for the workspace router.

Kept private to the `workspace` package — external callers should use the
HTTP surface or the FastAPI-generated OpenAPI schema, not these classes
directly."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# --- response models --------------------------------------------------------


class WorkspaceRoot(BaseModel):
    id: str


class WorkspaceRootsResponse(BaseModel):
    roots: list[WorkspaceRoot]


class TreeEntry(BaseModel):
    name: str
    # `other` collapses symlinks/sockets/fifos so the UI can render them as
    # non-previewable without enumerating every POSIX type.
    type: Literal["file", "dir", "other"]
    size: int


class WorkspaceTreeResponse(BaseModel):
    root: str
    path: str
    entries: list[TreeEntry]


class WorkspaceFileResponse(BaseModel):
    root: str
    path: str
    name: str
    # Null when we couldn't determine the size cheaply (e.g. the runtime
    # refused the read because the file exceeds the hard 10 MiB cap and we
    # already returned metadata-only).
    size: int | None
    kind: Literal["text", "markdown", "image", "binary"]
    # Set only for `text`/`markdown` previews that fit within the cap.
    content: str | None
    # Set only for `image` previews that fit within the cap; data: URL with
    # the appropriate image/* MIME and base64 payload.
    data_url: str | None
    # True iff a text preview was sliced because the file exceeds
    # TEXT_PREVIEW_CAP; always False for binary/image responses.
    truncated: bool
    # sha256 of the *on-disk bytes* (not the preview slice). Writers pass this
    # back as `base_sha` so the server can refuse stale writes with 409.
    # Null when we couldn't read the bytes (image cap exceeded / 10 MiB cap).
    sha256: str | None


class WorkspaceCreateRequest(BaseModel):
    root: str
    path: str
    content: str
    conversation_id: str = Field(min_length=1)


class WorkspaceUpdateRequest(BaseModel):
    root: str
    path: str
    content: str
    # 64-char lowercase hex — writer.py compares this against
    # hashlib.sha256(...).hexdigest(), so a malformed value should fail
    # validation (422) rather than masquerade as an edit conflict (409).
    base_sha: str = Field(pattern=r"^[0-9a-f]{64}$")
    conversation_id: str = Field(min_length=1)


class WorkspaceRenameRequest(BaseModel):
    root: str
    src: str
    dest: str
    conversation_id: str = Field(min_length=1)


class WorkspaceDeleteRequest(BaseModel):
    root: str
    path: str
    conversation_id: str = Field(min_length=1)


class WorkspaceWriteResponse(BaseModel):
    root: str
    path: str
    sha256: str
    # Whether a git commit was produced. False when the workspace root is not
    # a git repo; the file write still happened.
    committed: bool


class WorkspaceRenameResponse(BaseModel):
    root: str
    src: str
    dest: str
    committed: bool


class GitEntry(BaseModel):
    # Porcelain v1 two-char XY status code, e.g. " M", "??", "A ", "MM".
    status: str
    path: str


class WorkspaceGitResponse(BaseModel):
    root: str
    is_repo: bool
    branch: str | None
    dirty: bool
    entries: list[GitEntry]


# --- Plan 24: extended git surface ----------------------------------------


class GitDiffSummary(BaseModel):
    files: int
    insertions: int
    deletions: int


class GitDiffResponse(BaseModel):
    # `none` = no diff (either path is identical or there is nothing changed
    # at all). `binary` = git refuses to emit a patch; the patch field stays
    # null and the UI shows the summary only.
    kind: Literal["text", "binary", "none"]
    patch: str | None = None
    summary: GitDiffSummary
    # True when the patch body was truncated at the response cap. The summary
    # is still authoritative.
    truncated: bool = False


class GitBranch(BaseModel):
    name: str
    is_remote: bool
    last_commit_at: str | None


class GitBranchesResponse(BaseModel):
    current: str | None
    all: list[GitBranch]


class GitLogEntry(BaseModel):
    sha: str
    short_sha: str
    author: str
    subject: str
    committed_at: str


class GitCheckoutRequest(BaseModel):
    root: str
    branch: str = Field(min_length=1)
    create: bool = False
    # `force` only matters when the working tree is dirty: dirty checkout
    # without force returns 409; dirty checkout with force discards local
    # changes via `git checkout -f`, so it's gated by the destructive flag.
    force: bool = False


class GitPathsRequest(BaseModel):
    root: str
    paths: list[str] = Field(default_factory=list)


class GitDiscardRequest(BaseModel):
    root: str
    paths: list[str] = Field(default_factory=list)
    conversation_id: str = Field(min_length=1)


class GitCommitRequest(BaseModel):
    root: str
    message: str = Field(min_length=1)
    conversation_id: str = Field(min_length=1)
    # `all=true` stages tracked modifications before committing (git commit -a),
    # matching the "type a message and commit everything currently dirty" UX
    # path. Default false = commit whatever is already staged.
    all: bool = False


class GitFetchRequest(BaseModel):
    root: str


class GitPullRequest(BaseModel):
    root: str


class GitPushRequest(BaseModel):
    root: str
    set_upstream: bool = False


class GitOpResponse(BaseModel):
    ok: bool
    # stderr from the underlying git invocation. Surfaced verbatim so the UI
    # can show "permission denied" / "no upstream" without parsing.
    message: str = ""


class GitPullResponse(GitOpResponse):
    # Files git reported as conflicting (CONFLICT markers in stdout). Empty
    # on a clean pull; non-empty + ok=false on a conflict, returned as HTTP 200
    # so the UI doesn't have to parse a 4xx body to find the file list.
    conflicts: list[str] = Field(default_factory=list)
