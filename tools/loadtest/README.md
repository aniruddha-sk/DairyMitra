# Load test (k6)

Synthetic data only. Never point this at production.

Stack: MySQL 8 + the app under gunicorn (`gunicorn.conf.py`) + k6, in Docker.
Workload per virtual user (VU): 60% collection operators (save an entry every
3-6 s, open the collection page every 10th save), 15% owners (dashboard, 30-day
calculation/payment, receipt, every 10-20 s), 25% customers (dashboard, milk
history, every 10-20 s). 100 dairies (half litre-rate, half FAT/SNF), 60
vendors each, 30 days of history.

```bash
cd tools/loadtest
docker compose up -d db
docker compose run --rm -e DAIRYMITR_ENV=development app python tools/loadtest/seed_load.py
docker compose up -d app                       # APP_CPUS=4 WEB_CONCURRENCY=8 to resize
bash run_stage.sh 100 1 mytest                 # <VUs> <run number, unique per run> <label>
bash run_stage.sh 1000 2 mytest                # RAMP=5m HOLD=3m to change the profile
python summarize.py mytest
docker compose down -v                         # remove containers and data
```

On Windows Git Bash prefix the docker commands with `MSYS_NO_PATHCONV=1`.
To test a Railway staging service instead, run only k6 with `-e BASE=https://...`
after seeding the staging database with `seed_load.py`.
