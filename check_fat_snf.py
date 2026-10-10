from dotenv import load_dotenv
import os
import MySQLdb

load_dotenv()

host = os.getenv("DB_HOST") or os.getenv("MYSQL_HOST")
user = os.getenv("DB_USER") or os.getenv("MYSQL_USER")
password = os.getenv("DB_PASSWORD") or os.getenv("MYSQL_PASSWORD")
database = os.getenv("DB_NAME") or os.getenv("MYSQL_DB")

print("Connecting to database:", database)

conn = MySQLdb.connect(
    host=host,
    user=user,
    passwd=password,
    db=database
)

cursor = conn.cursor()
cursor.execute("SHOW TABLES LIKE %s", ("fat_snf%",))

print("FAT/SNF tables:", cursor.fetchall())

cursor.close()
conn.close()
