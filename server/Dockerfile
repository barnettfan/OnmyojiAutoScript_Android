FROM python:3.12-slim

# Keep Python output visible to Docker logs and avoid writing bytecode into the
# application volume at runtime.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY server/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY server /app

# The control plane persists device JSON and failure bundles under these
# directories.  A non-root runtime user owns the initial contents copied into
# the named Docker volumes on their first attachment.
RUN groupadd --system oas \
    && useradd --system --gid oas --home-dir /nonexistent --no-create-home oas \
    && mkdir -p /app/config/devices /app/log \
    && chown -R oas:oas /app

USER oas

EXPOSE 22270

# Host and port are supplied by docker-compose.yml so the process is reachable
# only from the reverse-proxy container.
CMD ["python", "main.py"]
