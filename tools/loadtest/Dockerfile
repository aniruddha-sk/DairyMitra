# Load-test image (NOT used for deployment). Build context: repository root.
FROM python:3.11.9-slim
RUN apt-get update && apt-get install -y --no-install-recommends         build-essential default-libmysqlclient-dev pkg-config     && rm -rf /var/lib/apt/lists/*
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["gunicorn", "-c", "gunicorn.conf.py", "app:app"]
