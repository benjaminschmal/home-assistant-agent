FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY server.py .
COPY hacs_status.py .
COPY test_client.py .

RUN useradd --create-home --uid 1000 agent \
    && chown -R agent:agent /app

USER agent

CMD ["python", "-c", "import asyncio, server, hacs_status; server.get_hacs_info = hacs_status.get_hacs_info; asyncio.run(server.main())"]
