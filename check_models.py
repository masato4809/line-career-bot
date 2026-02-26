import os
from dotenv import load_dotenv
from google import genai

load_dotenv()
client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

print("--- あなたのキーで使えるモデル一覧 ---")

for m in client.models.list():
    # supported_actions が無い/None のケースもあるので安全に
    actions = getattr(m, "supported_actions", None) or []
    if "generateContent" in actions:
        clean_name = m.name.replace("models/", "")
        print(clean_name)