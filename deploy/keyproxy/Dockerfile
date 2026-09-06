# The key proxy: a tiny service that holds the provider keys so the agent container never does.
FROM python:3.12-slim
RUN pip install --no-cache-dir "httpx==0.28.1" "aiohttp==3.11.18"
COPY deploy/keyproxy/proxy.py /app/proxy.py
USER nobody
CMD ["python", "/app/proxy.py"]
