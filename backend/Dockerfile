FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data \
    PORT=7860

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg nodejs \
    && (which node || ln -sf $(which nodejs) /usr/local/bin/node || true) \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 app

WORKDIR /app
COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py test_main.py ./
RUN mkdir -p /data && chown -R app:app /app /data
USER app
EXPOSE 7860
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-7860}"]
