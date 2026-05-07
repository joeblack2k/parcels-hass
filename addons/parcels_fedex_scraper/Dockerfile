FROM mcr.microsoft.com/playwright/python:v1.56.0-noble

ARG BUILD_VERSION=0.1.0
ARG BUILD_ARCH=amd64

LABEL \
    io.hass.version="${BUILD_VERSION}" \
    io.hass.type="app" \
    io.hass.arch="${BUILD_ARCH}"

WORKDIR /app

COPY app ./app
COPY run.sh /run.sh

RUN apt-get update \
    && apt-get install -y --no-install-recommends fluxbox novnc websockify x11vnc xvfb \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir "aiohttp>=3.10,<4" "cloudscraper==1.2.71" "playwright==1.56.0" \
    && chmod a+x /run.sh

ENV HOST=0.0.0.0
ENV PORT=8765
ENV FEDEX_SCRAPER_HEADLESS=true
ENV FEDEX_SCRAPER_TIMEOUT=45

EXPOSE 8765
EXPOSE 5900
EXPOSE 6080

CMD ["/run.sh"]
