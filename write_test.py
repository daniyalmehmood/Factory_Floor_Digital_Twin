from influxdb_client import InfluxDBClient, Point, SYNCHRONOUS
from dotenv import load_dotenv
from datetime import datetime, timezone
from pathlib import Path
import os, sys

# اقرأ .env من جذر المشروع (نفس مجلد هذا الملف)
BASE = Path(__file__).resolve().parent
load_dotenv(BASE / ".env")

URL    = "http://localhost:8086"
ORG    = "d1ba8ea0a6c7fc91"         # استخدمي Org ID
BUCKET = "twin"
TOKEN  = "GeJtGIlo8_Q7S0XtIUbItKawYm8fSVsIFMgLc391yP0T4_muJB5z8ZuTnzsevKOzbVqhZjhbNl7DDXgFb7HueQ=="

print("URL:", URL)
print("ORG:", ORG)
print("BUCKET:", BUCKET)
print("TOKEN prefix:", (TOKEN or "")[:6])

# تحقّق قبل ما نكمل
if not ORG or not TOKEN:
    sys.exit("❌ القيم ناقصة: تأكدي من INFLUX_ORG و INFLUX_TOKEN داخل .env")

client = InfluxDBClient(url=URL, token=TOKEN, org=ORG)
print("Health:", client.health().status)

write_api = client.write_api(write_options=SYNCHRONOUS)
p = (Point("telemetry_raw")
     .tag("line_id","L1").tag("machine_id","M01")
     .field("total_output", 123).field("uptime_sec", 456).field("status_code", 1)
     .time(datetime.now(timezone.utc)))

# مرّر الـorg صراحةً
write_api.write(bucket=BUCKET, org=ORG, record=p)
client.close()
print("✅ wrote 1 point")
