FROM python:3.14-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        chromium \
        fonts-dejavu-core \
        fonts-noto-color-emoji \
        rclone \
        tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --requirement /tmp/requirements.txt \
    && rm /tmp/requirements.txt

COPY export_sessions.py /usr/local/bin/export_sessions.py

ENTRYPOINT ["python3", "/usr/local/bin/export_sessions.py"]
