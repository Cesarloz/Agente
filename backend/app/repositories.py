from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import AppError


class Repository:
    def __init__(self, session: AsyncSession, model):
        self.session, self.model = session, model

    async def get(self, id: str, agent_id: str | None = None, lock: bool = False):
        query = select(self.model).where(self.model.id == id)
        if agent_id is not None:
            query = query.where(self.model.agent_id == agent_id)
        if lock:
            query = query.with_for_update()
        item = (await self.session.execute(query)).scalar_one_or_none()
        if item is None:
            raise AppError("Registro no encontrado", 404)
        return item

    async def list(self, *filters, limit: int = 1000):
        result = await self.session.scalars(
            select(self.model).where(*filters).order_by(self.model.created_at.desc()).limit(limit)
        )
        return list(result)

    async def add(self, **values):
        item = self.model(**values)
        self.session.add(item)
        await self.session.flush()
        return item


def serialize(item, exclude=()):
    return {c.name: getattr(item, c.name) for c in item.__table__.columns if c.name not in exclude}
