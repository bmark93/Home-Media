FROM docker:27-cli AS dockercli

FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# tzdata so the TZ env var (set from .env, same as every other service here)
# actually changes what datetime.now() returns - needed for the cleanup job's
# schedule to run at the wall-clock time the user picks, not UTC.
RUN apt-get update && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY app ./app

# docker CLI + compose plugin, so the linker can apply its own storage/Plex-claim
# changes by recreating the affected sibling containers via the shared Docker
# socket - see app/compose_control.py for why this needs the project directory
# mounted at the exact same absolute path inside this container as on the host.
COPY --from=dockercli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=dockercli /usr/local/libexec/docker/cli-plugins/docker-compose /usr/local/libexec/docker/cli-plugins/docker-compose

ENV PYTHONUNBUFFERED=1

# Default: run the web app. The qbit-init service overrides this command.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
