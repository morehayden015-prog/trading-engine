import urllib.request
import json

url = "https://discord.com/api/webhooks/1508078878155866242/VYL37Q6UKGvq1NcR7q0Bs6q5K_Fb7nHj3eo9bPMZVbJKYZnGu2tdaeMczLeSQrPj565_"
print(f"URL: {url[:60]}...")

payload = json.dumps({"content": "Test alert from Hayden Gold Bot!"}).encode()
req = urllib.request.Request(url, data=payload, headers={
    "Content-Type": "application/json",
    "User-Agent": "HaydenBot/1.0"
})

try:
    with urllib.request.urlopen(req, timeout=10) as resp:
        print(f"Success! Status: {resp.status}")
except Exception as e:
    print(f"Error: {e}")
