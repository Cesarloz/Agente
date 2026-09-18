import urllib.request

with urllib.request.urlopen("http://localhost:8000/health", timeout=3) as response:
    if response.status != 200:
        raise SystemExit(1)
