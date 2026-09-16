FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt

RUN useradd --system --uid 10001 --create-home rag \
    && mkdir -p /app/knowlegde /app/storage/documents \
    && chown -R rag:rag /app

COPY --chown=rag:rag app ./app

USER rag
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD ["python", "-m", "app.container_runtime", "--healthcheck"]

ENTRYPOINT ["python", "-m", "app.container_runtime"]
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
