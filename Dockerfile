FROM python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive

# Must match the uid the local Bot API server writes as, otherwise the bot cannot
# delete downloaded files from the shared bot-api volume.
ARG APP_UID=101
ARG APP_GID=101

RUN apt-get update \
    && apt-get install -y --no-install-recommends libchromaprint-tools \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -g "${APP_GID}" app \
    && useradd -u "${APP_UID}" -g "${APP_GID}" -M -d /app -s /usr/sbin/nologin app

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

COPY genre_map.yaml .
COPY app ./app

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["python", "-m", "app"]
