# Multi-stage: stage 1 bundles the pinned page-agent package for the
# browser; stage 2 is the Python shell. The app runs as a non-root
# user; the console + API serve on 8000.

FROM node:24-slim AS frontend
WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim AS app
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY pyproject.toml README.md constraints.txt ./
COPY src/ src/
COPY migrations/ migrations/
RUN pip install -r constraints.txt && pip install --no-deps .
COPY --from=frontend /build/src/page_agent_copilot/static/vendor/page-agent.bundle.js \
     src/page_agent_copilot/static/vendor/page-agent.bundle.js
RUN useradd --create-home copilot && mkdir -p /app/data && chown -R copilot:copilot /app
USER copilot
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"
CMD ["uvicorn", "page_agent_copilot.api:app", "--host", "0.0.0.0", "--port", "8000"]
