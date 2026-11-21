# syntax=docker/dockerfile:1
# endurance-planner API and web app. Multi-arch (Raspberry Pi arm64 and amd64):
#   docker buildx build --platform linux/arm64,linux/amd64 -t endurance-planner .
# Run (published on this machine only; reach it through `tailscale serve`):
#   docker run -d --name planner --restart on-failure \
#     -p 127.0.0.1:8000:8000 -v planner-data:/data \
#     --env-file /etc/endurance-planner.env endurance-planner
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLANNER_DATA_DIR=/data

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install ".[api,http,cli]" \
    && useradd --system --uid 10001 --home-dir /data --shell /usr/sbin/nologin planner \
    && install -d -o planner -g planner -m 700 /data

USER planner
VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2)"]
# Inside the container the server listens on all its interfaces; the token is required
# (it refuses to start without PLANNER_TOKEN). Publish the port on 127.0.0.1 only.
ENTRYPOINT ["endurance-planner", "serve"]
CMD ["--host", "0.0.0.0", "--port", "8000"]
