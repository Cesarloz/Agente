from app.errors import AppError
from app.models import Job
from app.repositories import Repository, serialize


class JobService:
    def __init__(self, session):
        self.session = session

    async def list(self, agent_id=None):
        rows = await Repository(self.session, Job).list(limit=5000)
        return [{**serialize(j, exclude=("payload", "dedup_key", "locked_until")), "agent_id": j.payload.get("agent_id")} for j in rows if not agent_id or j.payload.get("agent_id") == agent_id]

    async def retry(self, id, allow_resend=False):
        job = await Repository(self.session, Job).get(id, lock=True)
        if job.status not in {"FAILED", "NEEDS_REVIEW"}:
            raise AppError("Solo se pueden reintentar trabajos fallidos o pendientes de revisión", 409)
        if job.status == "NEEDS_REVIEW" and not allow_resend:
            raise AppError("Revisa el canal y autoriza el posible reenvío antes de reintentar", 409)
        job.payload = {**job.payload, "send_in_progress": False}
        job.status, job.error, job.attempts, job.locked_until = "PENDING", None, 0, None
        await self.session.commit()
        return {"status": "PENDING"}
