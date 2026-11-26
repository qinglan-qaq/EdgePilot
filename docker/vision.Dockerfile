FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

COPY vision-service/requirements.txt /app/requirements.txt

RUN pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu torch torchvision \
    && pip install --no-cache-dir -r /app/requirements.txt

COPY vision-service/app /app/app
COPY vision-service/scripts /app/scripts

# 权重不打进镜像：挂载 vision_weights 命名卷，缺失时 /health 报 degraded。
ENV VISION_MODEL_PATH=/app/weights/yolo11n.pt \
    VISION_JOBS_DIR=/data/vision/jobs

EXPOSE 8001

HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=20 \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://localhost:8001/health')" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8001"]
