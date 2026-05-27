import time

from sqlalchemy import asc, select
from sqlalchemy.ext.asyncio import AsyncEngine

from hermes.repository.models import Attachment
from hermes.schema import attachments as t_attachments


def _row_to_attachment(row) -> Attachment:
    return Attachment(
        id=row.id,
        conversation_id=row.conversation_id,
        message_id=row.message_id,
        filename=row.filename,
        content_type=row.content_type,
        size=row.size,
        storage_path=row.storage_path,
        created_at=row.created_at,
    )


async def create(
    engine: AsyncEngine,
    *,
    conversation_id: int,
    filename: str,
    content_type: str,
    size: int,
    storage_path: str,
    ts: int | None = None,
) -> Attachment:
    now = ts if ts is not None else int(time.time())
    async with engine.begin() as conn:
        result = await conn.execute(
            t_attachments.insert()
            .values(
                conversation_id=conversation_id,
                message_id=None,
                filename=filename,
                content_type=content_type,
                size=size,
                storage_path=storage_path,
                created_at=now,
            )
            .returning(*t_attachments.c)
        )
        row = result.first()
    if row is None:
        raise RuntimeError("INSERT into attachments did not yield a row")
    return _row_to_attachment(row)


async def get(engine: AsyncEngine, attachment_id: int) -> Attachment | None:
    async with engine.connect() as conn:
        result = await conn.execute(
            select(t_attachments).where(t_attachments.c.id == attachment_id)
        )
        row = result.first()
    return _row_to_attachment(row) if row is not None else None


async def list_by_conversation(
    engine: AsyncEngine, conversation_id: int
) -> list[Attachment]:
    async with engine.connect() as conn:
        result = await conn.execute(
            select(t_attachments)
            .where(t_attachments.c.conversation_id == conversation_id)
            .order_by(asc(t_attachments.c.id))
        )
        rows = result.all()
    return [_row_to_attachment(r) for r in rows]


async def list_by_message(
    engine: AsyncEngine, message_id: int
) -> list[Attachment]:
    async with engine.connect() as conn:
        result = await conn.execute(
            select(t_attachments)
            .where(t_attachments.c.message_id == message_id)
            .order_by(asc(t_attachments.c.id))
        )
        rows = result.all()
    return [_row_to_attachment(r) for r in rows]


async def link_to_message(
    engine: AsyncEngine,
    *,
    attachment_ids: list[int],
    message_id: int,
    conversation_id: int,
) -> int:
    """Attach the staged uploads to a freshly-persisted user message.

    Only rows that belong to the same conversation and are still unlinked
    (`message_id IS NULL`) are updated — so an id from another conversation
    or an already-sent attachment silently matches nothing. The caller
    validates ownership up front and surfaces the error; this is the
    last-line guard. Returns the number of rows linked.
    """
    if not attachment_ids:
        return 0
    async with engine.begin() as conn:
        result = await conn.execute(
            t_attachments.update()
            .where(
                t_attachments.c.id.in_(attachment_ids),
                t_attachments.c.conversation_id == conversation_id,
                t_attachments.c.message_id.is_(None),
            )
            .values(message_id=message_id)
        )
    return result.rowcount or 0
