FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 \
    KOSH_HOST=0.0.0.0 KOSH_NO_BROWSER=1 KOSH_SECRET_STORE=file

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY config.py main.py ./
COPY finance ./finance

# Your ledger, mailbox settings and secrets live in /app/data: mount a volume there so they outlive the container.
RUN useradd --create-home --uid 10001 kosh && mkdir -p /app/data && chown kosh /app/data
USER kosh
VOLUME /app/data
EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import socket; socket.create_connection(('127.0.0.1', 8765), 2)"
ENTRYPOINT ["python", "main.py"]
CMD ["ui"]
