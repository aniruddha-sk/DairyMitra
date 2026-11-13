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
# Recycle workers periodically to bound memory growth. 1000 was too low:
# under load (measured at 500 concurrent users) every worker restarted every
# few seconds, each restart re-imported the app (~2 s CPU) and dropped
# in-flight keep-alive connections.
max_requests = int(os.getenv("GUNICORN_MAX_REQUESTS", 10000))
max_requests_jitter = int(os.getenv("GUNICORN_MAX_REQUESTS_JITTER", 1000))
# Import the app once in the master and fork workers from it: workers share
# the imported code's memory (copy-on-write) and a recycled worker starts in
# milliseconds instead of re-importing. No database connection or thread pool
# exists at import time, so forking is safe. GUNICORN_PRELOAD=0 turns it off.
preload_app = os.getenv("GUNICORN_PRELOAD", "1") != "0"
accesslog = "-"
errorlog = "-"
loglevel = os.getenv("GUNICORN_LOGLEVEL", "info")
# Trust X-Forwarded-* only via the app's TRUST_PROXY_HOPS (ProxyFix).
forwarded_allow_ips = "127.0.0.1"
