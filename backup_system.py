import os
import subprocess
import gzip
import shutil
from datetime import datetime
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

load_dotenv()

DB_HOST = os.getenv("MYSQL_HOST")
DB_PORT = int(os.getenv("MYSQL_PORT", 3306))
DB_USER = os.getenv("MYSQL_USER")
DB_PASS = os.getenv("MYSQL_PASSWORD")
DB_NAME = os.getenv("MYSQL_DB")

BACKUP_DIR = "backups"

os.makedirs(BACKUP_DIR, exist_ok=True)


# -------------------------------------------------
# TIME (KOLKATA / IST)
# -------------------------------------------------

def get_timestamp():
    return datetime.now(
        ZoneInfo("Asia/Kolkata")
    ).strftime("%Y%m%d_%H%M%S")


# -------------------------------------------------
# CREATE MYSQL BACKUP (FAST + COMPRESSED)
# -------------------------------------------------

def create_backup(backup_type="manual", user_id=1):

    timestamp = get_timestamp()

    sql_file = os.path.join(
        BACKUP_DIR,
        f"{backup_type}_user{user_id}_{timestamp}_IST.sql"
    )

    gzip_file = sql_file + ".gz"

    command = [
        "mysqldump",
        "--single-transaction",
        "--quick",
        "--skip-lock-tables",
        "--set-gtid-purged=OFF",
        "-h", DB_HOST,
        "-P", str(DB_PORT),
        "-u", DB_USER,
        f"-p{DB_PASS}",
        DB_NAME
    ]

    try:

        print("Starting database backup...")

        with open(sql_file, "w") as f:
            subprocess.run(command, stdout=f, check=True)

        print("Compressing backup...")

        with open(sql_file, "rb") as f_in:
            with gzip.open(gzip_file, "wb") as f_out:
                shutil.copyfileobj(f_in, f_out)

        os.remove(sql_file)

        cleanup_old_backups()

        print("Backup created:", gzip_file)

        return os.path.basename(gzip_file)

    except subprocess.CalledProcessError as e:

        print("Backup failed:", e)

        if os.path.exists(sql_file):
            os.remove(sql_file)

        raise


# -------------------------------------------------
# FULL DATABASE BACKUP (ADMIN)
# -------------------------------------------------

def create_full_backup():

    timestamp = get_timestamp()

    sql_file = os.path.join(
        BACKUP_DIR,
        f"admin_full_{timestamp}_IST.sql"
    )

    gzip_file = sql_file + ".gz"

    command = [
        "mysqldump",
        "--single-transaction",
        "--quick",
        "--skip-lock-tables",
        "--set-gtid-purged=OFF",
        "-h", DB_HOST,
        "-P", str(DB_PORT),
        "-u", DB_USER,
        f"-p{DB_PASS}",
        DB_NAME
    ]

    with open(sql_file, "w") as f:
        subprocess.run(command, stdout=f, check=True)

    with open(sql_file, "rb") as f_in:
        with gzip.open(gzip_file, "wb") as f_out:
            shutil.copyfileobj(f_in, f_out)

    os.remove(sql_file)

    cleanup_old_backups()

    return os.path.basename(gzip_file)


# -------------------------------------------------
# RESTORE BACKUP
# -------------------------------------------------

def restore_backup(filename):

    filepath = os.path.join(BACKUP_DIR, filename)

    if not os.path.exists(filepath):
        raise Exception("Backup file not found")

    # -------------------------------------------------
    # SAFETY BACKUP BEFORE RESTORE
    # -------------------------------------------------

    safety_name = create_backup("safety", 1)
    print("Safety backup created:", safety_name)

    temp_sql = filepath.replace(".gz", ".sql")

    print("Extracting backup...")

    with gzip.open(filepath, "rb") as f_in:
        with open(temp_sql, "wb") as f_out:
            shutil.copyfileobj(f_in, f_out)

    command = [
        "mysql",
        "--max_allowed_packet=512M",
        "-h", DB_HOST,
        "-P", str(DB_PORT),
        "-u", DB_USER,
        f"-p{DB_PASS}",
        DB_NAME
    ]

    print("Restoring database...")

    with open(temp_sql, "rb") as sql_file:
        subprocess.run(command, stdin=sql_file, check=True)

    os.remove(temp_sql)

    print("Restore completed")

    return True


# -------------------------------------------------
# CLEANUP OLD BACKUPS
# -------------------------------------------------

def cleanup_old_backups():

    files = sorted(
        os.listdir(BACKUP_DIR),
        key=lambda x: os.path.getmtime(os.path.join(BACKUP_DIR, x))
    )

    while len(files) > 30:

        oldest = files[0]
        filepath = os.path.join(BACKUP_DIR, oldest)

        try:
            os.remove(filepath)
            print("Deleted old backup:", oldest)
        except Exception as e:
            print("Delete error:", e)

        files.pop(0)


# -------------------------------------------------
# BACKUP HISTORY
# -------------------------------------------------

def list_backups():

    files = sorted(
        os.listdir(BACKUP_DIR),
        key=lambda x: os.path.getmtime(os.path.join(BACKUP_DIR, x)),
        reverse=True
    )

    return files