FROM mcr.microsoft.com/playwright/python:v1.56.0-noble

WORKDIR /app

COPY pyproject.toml README.md ./
COPY app ./app

RUN pip install --no-cache-dir .

ENV PORT=8765
ENV HOST=0.0.0.0
ENV FEDEX_SCRAPER_HEADLESS=true
ENV FEDEX_SCRAPER_TIMEOUT=45

EXPOSE 8765

CMD ["python", "-m", "app.main"]
