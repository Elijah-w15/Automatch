# Multi-stage build. Each instruction is a cached layer reused when its inputs
# are unchanged.

# Base image: slim Python on minimal Linux. Named so the advanced stage builds
# on it; compose selects the stage per service.
FROM python:3.12-slim AS base

# Working directory inside the container, created if absent.
WORKDIR /app

# Copy requirements first and install. Isolating deps in their own layer keeps
# code edits from re-running pip install.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application code.
COPY automatch/ ./automatch/

# Default ollama endpoint. host.docker.internal resolves to the host machine;
# docker-compose.yml maps it on Linux.
ENV OLLAMA_HOST=http://host.docker.internal:11434

# Entrypoint runs the automatch module; CMD args are appended and overridable.
ENTRYPOINT ["python", "-m", "automatch"]
CMD ["run"]

# Advanced stage: base plus discord.py for the bot. Built only by the compose
# "bot" service.
FROM base AS advanced
RUN pip install --no-cache-dir discord.py
