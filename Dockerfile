FROM node:22-slim AS frontend-build

WORKDIR /frontend
COPY frontend/package*.json ./
RUN npm ci
COPY frontend ./
RUN npm run build

FROM python:3.12-slim AS runtime

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

RUN pip install -r requirements.txt

COPY . .
COPY --from=frontend-build /frontend/dist ./frontend/dist

RUN groupadd --system mememeow \
    && useradd --system --create-home --gid mememeow --shell /bin/bash mememeow \
    && chown -R mememeow:mememeow /app

ENV HOME=/home/mememeow \
    PYTHONUNBUFFERED=1

USER mememeow

CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8275"]
