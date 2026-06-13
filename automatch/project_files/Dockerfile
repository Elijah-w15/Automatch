# Multi-stage build. `base` is the core scrape/score/rank pipeline; `advanced`
# adds discord.py for the optional ATS bot. docker-compose selects the target.
FROM python:3.12-slim AS base
WORKDIR /app

# Install dependencies before copying code so the pip layer stays cached across
# code edits (deps change rarely, code changes constantly).
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY automatch/ ./automatch/

# Inside the container "localhost" is the container itself; reach the host's
# ollama via host.docker.internal (mapped to the gateway on Linux in compose).
ENV OLLAMA_HOST=http://host.docker.internal:11434

# Args after `docker compose run automatch` are appended (e.g. `run -r 5`).
ENTRYPOINT ["python", "-m", "automatch"]
CMD ["run"]

# Advanced image: base + discord.py. Only the compose "bot" service builds it,
# so the basic image stays lean.
FROM base AS advanced
COPY requirements-advanced.txt .
RUN pip install --no-cache-dir -r requirements-advanced.txt
