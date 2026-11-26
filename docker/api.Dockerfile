FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app/backend

COPY backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/app ./app
COPY backend/migrations ./migrations
COPY backend/alembic.ini ./alembic.ini

# 保留清理脚本放进镜像，离线部署可直接在容器内 dry-run / execute；
# 脚本按 /app/scripts/retention.py 推算 REPO_ROOT=/app、BACKEND_DIR=/app/backend。
COPY scripts/retention.py /app/scripts/retention.py

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
