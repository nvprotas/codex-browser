FROM python:3.12-slim-bookworm@sha256:d97792894a6a4162cae14da44542a83c75e56c77a27b92d58f3f83b7bc961292

WORKDIR /app

ENV DEBIAN_FRONTEND=noninteractive \
    DISPLAY=:99 \
    XDG_RUNTIME_DIR=/tmp/runtime-chromium

RUN apt-get update && apt-get install -y --no-install-recommends \
    chromium \
    xvfb \
    x11vnc \
    fluxbox \
    novnc \
    socat \
    xauth \
    dbus-x11 \
    procps \
    curl \
    ca-certificates \
    fonts-liberation \
    xdg-utils \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir "websockify>=0.10.0,<0.13.0"

COPY browser/entrypoint.sh /usr/local/bin/browser-sidecar.sh
RUN chmod +x /usr/local/bin/browser-sidecar.sh

EXPOSE 6901 9223

CMD ["/usr/local/bin/browser-sidecar.sh"]
