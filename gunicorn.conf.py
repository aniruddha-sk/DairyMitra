"""Gunicorn settings for production (Procfile: gunicorn -c gunicorn.conf.py app:app)."""
import multiprocessing
import os

bind = f"0.0.0.0:{os.getenv('PORT', '8000')}"
workers = int(os.getenv("WEB_CONCURRENCY", min(multiprocessing.cpu_count() * 2 + 1, 4)))
threads = int(os.getenv("GUNICORN_THREADS", 2))
# Reports/receipts for long ranges and bank-report generation can take a few
# seconds; 60s is generous while still killing stuck workers.
timeout = int(os.getenv("GUNICORN_TIMEOUT", 60))
graceful_timeout = 30
keepalive = 5
# Recycle workers periodically to bound memory growth.
max_requests = 1000
max_requests_jitter = 100
accesslog = "-"
errorlog = "-"
loglevel = os.getenv("GUNICORN_LOGLEVEL", "info")
# Trust X-Forwarded-* only via the app's TRUST_PROXY_HOPS (ProxyFix).
forwarded_allow_ips = "127.0.0.1"
