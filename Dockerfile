FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server.py build_index.py price_sweep.py ./
ENV WORKBENCH_DIR=/app
# Bind to all interfaces inside the container so the mapped port is reachable
# (the app defaults to 127.0.0.1, which is correct behind a host reverse proxy).
ENV WORKBENCH_HOST=0.0.0.0
EXPOSE 4036
# server.py creates an empty index schema on first run if index.sqlite is absent,
# so tools/list works out of the box; add your own scans.jsonl + build_index.py for data.
CMD ["python", "server.py"]
