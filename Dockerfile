# Sophia's voice server, for a free hosting instance such as Render's.
#
#   docker build -t sophia .
#   docker run -p 10000:10000 --env-file .env sophia
#
# API keys are never baked into the image: they come from the host's
# environment settings, or --env-file locally.

FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Run as an ordinary user, not root.
RUN useradd --create-home --uid 1000 sophia
WORKDIR /home/sophia/app

COPY --chown=sophia requirements-deploy.txt .
RUN pip install -r requirements-deploy.txt

COPY --chown=sophia src ./src
COPY --chown=sophia web ./web
COPY --chown=sophia data/practice_info ./data/practice_info
COPY --chown=sophia data/seed ./data/seed

# Directories created by COPY above can belong to root. The server writes
# each tester's copy of the demo data under data/sessions, so it must own it.
RUN mkdir -p data/sessions && chown -R sophia:sophia /home/sophia/app

USER sophia

# Hosts set PORT. Each tester's demo data is created fresh under data/ when
# they first call, so nothing needs to persist between restarts.
EXPOSE 10000
CMD ["sh", "-c", "uvicorn sophia.voice_app:app --app-dir src --host 0.0.0.0 --port ${PORT:-10000}"]
