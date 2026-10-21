"""Files and links share a revision, scope, and complete-query counts."""
from typing import Literal

from pydantic import Field

from .base import JsonValue, Params, Result
from .registry import method


class IndexParams(Params):
    profile: str = Field(min_length=1, max_length=128, strict=True)
    session_id: str | None = Field(default=None, min_length=1, max_length=512, strict=True)
    workspace_id: str | None = Field(default=None, min_length=1, max_length=512, strict=True)
    q: str = Field(default='', max_length=256, strict=True)
    cursor: str | None = Field(default=None, max_length=8192, strict=True)
    snapshot_id: str | None = Field(default=None, max_length=128, strict=True)
    limit: int = Field(default=50, ge=1, le=200, strict=True)
    author: Literal['all', 'agents', 'me'] = 'all'


class FilesIndexParams(IndexParams):
    category: Literal['all', 'media', 'files'] = 'all'
    include_workspace: bool = Field(default=False, strict=True)
    directory_path: str | None = Field(default=None, max_length=4096, strict=True)


class IndexTotals(Result):
    media: int | None
    files: int | None
    links: int | None
    messages: int | None


class IndexCoverage(Result):
    complete: bool
    refresh_pending: bool
    updated_at: float | None


class FileCollection(Result):
    id: str
    title: str
    member_ids: list[str]
    picked_id: str | None


class FileIndexItem(Result):
    # DeliverableReference is producer-owned; preserve its additive version metadata.
    model_config = {'extra': 'allow'}
    id: str
    profile: str
    kind: str
    display_name: str
    stored_session_id: str | None
    message_id: str | None
    source_chat_title: str | None
    observed_at: float | None
    workspace_id: str | None
    relative_path: str | None = None
    url: str | None = None
    author_kind: Literal['agent', 'me', 'unknown']
    author_profile: str | None
    is_directory: bool
    mention_text: str | None
    collection: FileCollection | None
    occurrences: list[JsonValue] = Field(default_factory=list)


class LinkIndexItem(Result):
    id: str
    profile: str
    url: str
    title: str | None
    stored_session_id: str
    message_id: str
    source_chat_title: str | None
    observed_at: float | None
    workspace_id: str | None
    author_kind: Literal['agent', 'me', 'unknown']
    author_profile: str | None
    tag: Literal['preview', 'internal'] | None
    mention_text: str | None


class IndexResult(Result):
    schema_version: Literal[1]
    profile: str
    snapshot_id: str
    index_revision: str
    next_cursor: str | None
    totals: IndexTotals
    coverage: IndexCoverage


class FilesIndexResult(IndexResult):
    items: list[FileIndexItem]


class LinksIndexResult(IndexResult):
    items: list[LinkIndexItem]


method('files.index', params=FilesIndexParams, result=FilesIndexResult)
method('links.index', params=IndexParams, result=LinksIndexResult)
