import os
from dotenv import load_dotenv
from google import genai

# Load .env
load_dotenv()

# Create Gemini client
client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

# Send test message
response = client.models.generate_content(
    model="gemini-3.8-flash",
    contents="Hello! Introduce yourself as DairyMitr AI in one short sentence."
)

print("\n==============================")
print("DairyMitr AI Response:")
print("==============================")
print(response.text)