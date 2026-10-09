# DairyMitr

Dairy milk-collection management (Flask + MySQL): vendors, litre-rate or
FAT/SNF-matrix milk collection, Food Sack, Advance ledger, payments,
Marathi receipts, staff and customer portals.

* **What changed in 2.0, business rules, deployment and rollback:** [docs/DAIRYMITRA_2.0.md](docs/DAIRYMITRA_2.0.md)
* Configuration: copy [.env.example](.env.example) to `.env` (local only)
* Schema changes: `python -m migrations` (re-runnable; `--status`, `--dry-run`)
* Run locally: `python app.py`; production: `gunicorn -c gunicorn.conf.py app:app`
* Tests: `pip install -r requirements-dev.txt`, then
  `TEST_MYSQL_HOST=... pytest tests` (use a **disposable** MySQL server)
