FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server.py build_index.py price_sweep.py ./
ENV WORKBENCH_DIR=/app
EXPOSE 4036
# server.py creates an empty index schema on first run if index.sqlite is absent,
# so tools/list works out of the box; add your own scans.jsonl + build_index.py for data.
CMD ["python", "server.py"]
