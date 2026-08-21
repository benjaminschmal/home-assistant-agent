FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY agent.py .

RUN useradd --create-home --uid 1000 agent \
    && chown -R agent:agent /app

USER agent

EXPOSE 8080

CMD ["python", "/app/agent.py"]
