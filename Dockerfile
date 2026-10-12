# Portable Linux/amd64 portal; account and identity settings are runtime-only.
FROM python:3.12.15-alpine3.23@sha256:9ce5a4e59b833c002e1c4196c60ec0b38977e3235d626298ba7ffe2e64f5a30c
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt ./
RUN apk upgrade --no-cache \
    && pip install --only-binary=:all: --no-cache-dir -r requirements.txt \
    && addgroup -g 10001 portal \
    && adduser -D -H -u 10001 -G portal portal \
    && chmod 1777 /tmp
COPY app.py ./
COPY portal/templates/ ./portal/templates/
USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"]
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "1", "--threads", "1", "--worker-class", "sync", "--max-requests", "0", "--timeout", "60", "--graceful-timeout", "30", "--access-logfile", "-", "--error-logfile", "-", "--access-logformat", "%(m)s %(U)s %(s)s %(L)s", "app:app"]
