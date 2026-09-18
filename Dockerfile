FROM python:3.12-slim AS application
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
RUN useradd --uid 10001 --create-home agente
COPY pyproject.toml requirements.lock ./
COPY backend ./backend
RUN pip install --no-cache-dir -r requirements.lock && pip install --no-deps .
COPY admin ./admin
COPY .streamlit ./.streamlit
COPY alembic.ini ./
COPY migrations ./migrations
COPY scripts ./scripts
RUN mkdir -p /app/storage && chown -R agente:agente /app
USER agente
EXPOSE 8000 8501
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
