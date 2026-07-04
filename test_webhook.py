import urllib.request
import json

payload = json.dumps({
    "secret": "hayden_private_key",
    "action": "buy",
    "symbol": "XAUUSD",
    "price": "2345.50",
    "sl": "2338.00",
    "tp1": "2368.00",
    "tp2": "2385.00",
    "session": "london",
    "tf": "5",
    "score": 8,
    "setup": "sweep+bos+fvg+confirm"
}).encode()

req = urllib.request.Request(
    "http://localhost:8000/webhook",
    data=payload,
    headers={"Content-Type": "application/json"}
)

with urllib.request.urlopen(req, timeout=15) as resp:
    result = json.loads(resp.read())
    print(f"Score : {result['score']}")
    print(f"Grade : {result['grade']}")
    print(f"Alerts: {result['alerts']}")
