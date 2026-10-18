FROM node:22-alpine AS web
WORKDIR /web
COPY web/package.json web/package-lock.json* ./
RUN npm install
COPY web/ ./
RUN npm run build

FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1
COPY pyproject.toml README.md ./
COPY rationale ./rationale
COPY eval ./eval
COPY action ./action
COPY config ./config
COPY data ./data
RUN pip install --no-cache-dir .
COPY --from=web /web/dist ./web/dist
EXPOSE 8000
CMD ["sh", "-c", "uvicorn rationale.api.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
