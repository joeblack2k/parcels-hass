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

RUN pip install --no-cache-dir "aiohttp>=3.10,<4" "playwright==1.56.0" \
    && chmod a+x /run.sh

ENV HOST=0.0.0.0
ENV PORT=8765
ENV FEDEX_SCRAPER_HEADLESS=true
ENV FEDEX_SCRAPER_TIMEOUT=45

EXPOSE 8765

CMD ["/run.sh"]
