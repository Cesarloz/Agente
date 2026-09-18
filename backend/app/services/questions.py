from collections import Counter
from difflib import SequenceMatcher
import re
import unicodedata

from sqlalchemy import select

from app.errors import AppError
from app.models import Document, FAQ, Question
from app.repositories import Repository, serialize
from app.services.knowledge import FAQService


class QuestionService:
    def __init__(self, session):
        self.session = session

    async def list(self, agent_id=None, unanswered=False, out_of_scope=False, limit=1000):
        filters = []
        if agent_id:
            filters.append(Question.agent_id == agent_id)
        if unanswered:
            filters.append(Question.answered.is_(False))
        if out_of_scope:
            filters.append(Question.scope_status == "OUT_OF_SCOPE")
        return [{**serialize(q), "timestamp": q.created_at} for q in await Repository(self.session, Question).list(*filters, limit=limit)]

    async def to_faq(self, id, data):
        question = await Repository(self.session, Question).get(id)
        answer = data.answer or (question.response if question.answered else "")
        if not answer.strip():
            raise AppError("Escribe una respuesta verificada para crear la FAQ")
        return await FAQService(self.session).save(question.agent_id, {"question": question.question, "answer": answer, "tags": data.tags, "priority": data.priority})


class AnalyticsService:
    def __init__(self, session):
        self.session = session

    async def summary(self, agent_id=None):
        query = select(Question)
        if agent_id:
            query = query.where(Question.agent_id == agent_id)
        # Streaming iteration avoids truncating totals at the table viewer's page limit.
        stream = await self.session.stream_scalars(query.order_by(Question.created_at))
        total = unanswered = out = 0
        days, faqs, documents = Counter(), Counter(), Counter()
        candidates = []
        async for q in stream:
            total += 1
            unanswered += not q.answered
            out += q.scope_status == "OUT_OF_SCOPE"
            days[q.created_at.date().isoformat()] += 1
            faqs.update(q.faq_ids)
            documents.update(q.document_ids)
            # Similarity grouping is explicitly a bounded lexical heuristic, not an embedding claim.
            if not q.answered:
                candidates.append((q.id, q.question))
                if len(candidates) > 500:
                    candidates.pop(0)
        groups = []
        for id, question in candidates:
            normalized = re.sub(r"\W+", " ", unicodedata.normalize("NFKD", question.casefold()).encode("ascii", "ignore").decode()).strip()
            for group in groups:
                if SequenceMatcher(None, normalized, group["normalized"]).ratio() >= 0.82:
                    group["question_ids"].append(id)
                    group["count"] += 1
                    break
            else:
                groups.append({"question": question, "count": 1, "question_ids": [id], "normalized": normalized})
        async def top(counter, model, name):
            result = []
            for id, count in counter.most_common(10):
                row = await self.session.get(model, id)
                result.append({"id": id, "count": count, "name": getattr(row, name) if row else "Fuente eliminada"})
            return result
        return {"total_questions": total, "unanswered": unanswered, "out_of_scope": out, "by_day": [{"day": d, "count": c} for d, c in sorted(days.items())], "top_faqs": await top(faqs, FAQ, "question"), "top_documents": await top(documents, Document, "name"), "similar_questions": [{k: v for k, v in g.items() if k != "normalized"} for g in sorted(groups, key=lambda g: g["count"], reverse=True) if g["count"] > 1], "similarity_method": "Similitud léxica ≥0.82 sobre las últimas 500 preguntas sin respuesta; revisar antes de crear FAQ", "usage_definition": "Fuente recuperada e incluida en el contexto; no certifica que haya fundamentado la respuesta"}
