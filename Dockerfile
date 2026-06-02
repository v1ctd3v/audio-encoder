# Stage 1: install Python dependencies into an isolated venv
FROM python:3.13-alpine AS builder

WORKDIR /build
COPY requirements.txt .
RUN python -m venv /opt/venv && \
    /opt/venv/bin/pip install --no-cache-dir --upgrade pip && \
    /opt/venv/bin/pip install --no-cache-dir -r requirements.txt


# Stage 2: minimal runtime image (Alpine — far fewer CVEs than slim)
FROM python:3.13-alpine

# ffmpeg only; apk cache is never written to disk
RUN apk add --no-cache ffmpeg

# Dedicated non-root user: no password, no home dir, no shell
RUN adduser -D -H -s /sbin/nologin -u 1001 appuser

# Copy venv from builder (build tools never reach this layer)
COPY --from=builder /opt/venv /opt/venv

WORKDIR /app

COPY main.py .
COPY static/ ./static/

# Storage dirs owned by appuser; root fs is mounted read-only at runtime
RUN mkdir -p storage/uploads storage/converted && \
    chown -R appuser:appuser /app

USER appuser

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8000

# Single worker: the job registry is in-memory and cannot be shared across workers.
# Scale horizontally with multiple containers behind a load balancer instead.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
