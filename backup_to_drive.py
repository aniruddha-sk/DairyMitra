import os
import pickle
import subprocess
from datetime import datetime

from dotenv import load_dotenv

from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request


# ---------------------------------------------------
# LOAD ENV VARIABLES
# ---------------------------------------------------

load_dotenv()

DB_HOST = os.getenv("MYSQL_HOST")
DB_PORT = os.getenv("MYSQL_PORT")
DB_USER = os.getenv("MYSQL_USER")
DB_PASS = os.getenv("MYSQL_PASSWORD")
DB_NAME = os.getenv("MYSQL_DB")


# ---------------------------------------------------
# GOOGLE DRIVE CONFIG
# ---------------------------------------------------

SCOPES = ['https://www.googleapis.com/auth/drive.file']

DRIVE_FOLDER_ID = "1x1VZSlVEX3ZZR0cSeJGTdXrIx4RltVhD"


# ---------------------------------------------------
# CREATE MYSQL BACKUP
# ---------------------------------------------------

def create_backup():
    """Create MySQL backup using mysqldump"""

    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    filename = f"backup_{timestamp}.sql"

    print("🔹 Creating database backup...")

    command = [
        "mysqldump",
        "-h", DB_HOST,
        "-P", DB_PORT,
        "-u", DB_USER,
        f"-p{DB_PASS}",
        DB_NAME
    ]

    with open(filename, "w") as file:
        subprocess.run(command, stdout=file)

    print(f"✅ Backup created: {filename}")

    return filename


# ---------------------------------------------------
# GOOGLE DRIVE AUTH
# ---------------------------------------------------

def get_drive_service():
    """Authenticate with Google Drive"""

    creds = None

    if os.path.exists("token.pickle"):
        with open("token.pickle", "rb") as token:
            creds = pickle.load(token)

    if not creds or not creds.valid:

        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())

        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                 "client_secret.json",
                SCOPES
            )
            creds = flow.run_local_server(port=0)

        with open("token.pickle", "wb") as token:
            pickle.dump(creds, token)

    return build("drive", "v3", credentials=creds)


# ---------------------------------------------------
# UPLOAD FILE TO GOOGLE DRIVE
# ---------------------------------------------------

def upload_to_drive(file_name):
    """Upload backup file to Google Drive"""

    print("☁ Uploading backup to Google Drive...")

    service = get_drive_service()

    file_metadata = {
        "name": file_name,
        "parents": [DRIVE_FOLDER_ID]
    }

    media = MediaFileUpload(file_name, resumable=True)

    service.files().create(
        body=file_metadata,
        media_body=media,
        fields="id"
    ).execute()

    print("✅ Backup uploaded successfully!")


# ---------------------------------------------------
# MAIN BACKUP FLOW
# ---------------------------------------------------

def run_backup():

    backup_file = create_backup()

    upload_to_drive(backup_file)


# ---------------------------------------------------
# RUN SCRIPT
# ---------------------------------------------------

if __name__ == "__main__":
    run_backup()