from dotenv import load_dotenv
import os
import json
import urllib.request
import urllib.error
import hashlib

load_dotenv()

api_key = os.getenv("GROQ_API_KEY")

if not api_key:
    raise RuntimeError("GROQ_API_KEY is not loaded")

fingerprint = hashlib.sha256(
    api_key.encode()
).hexdigest()[:12]

print(f"Key fingerprint: sha256:{fingerprint}")

url = "https://api.groq.com/openai/v1/models"

request = urllib.request.Request(
    url,
    headers={
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "User-Agent": "MailMind/1.0",
    },
    method="GET",
)

try:
    with urllib.request.urlopen(request, timeout=15) as response:
        body = response.read().decode("utf-8")

        print(f"\nHTTP: {response.status}")

        data = json.loads(body)

        print("Qwen model found:")

        found = False

        for model in data.get("data", []):
            model_id = model.get("id")

            if model_id == "qwen/qwen3.8-27b":
                print("YES - qwen/qwen3.8-27b")
                found = True

        if not found:
            print("NO - qwen/qwen3.8-27b was not returned")

except urllib.error.HTTPError as e:
    body = e.read().decode("utf-8", errors="replace")

    print(f"\nHTTP: {e.code}")
    print("Response:")
    print(body)

except Exception as e:
    print("\nERROR:")
    print(repr(e))