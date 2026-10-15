FROM python:3.12-slim
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py mcp_server.py ./
ENV HIVE_DB=/data/hive.db
VOLUME ["/data"]
EXPOSE 8000
# single worker on purpose: the rate limiter is in-memory
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
