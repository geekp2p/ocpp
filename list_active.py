import json
import sys
from typing import List

import requests

API_BASE = "http://45.136.236.186:8080"
API_KEY = "changeme-123"


def fetch_active() -> List[dict]:
    url = f"{API_BASE}/api/v1/active"
    try:
        resp = requests.get(url, headers={"X-API-Key": API_KEY}, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        return data.get("sessions", [])
    except Exception as exc:
        print("error:", exc)
        return []


def main() -> None:
    sessions = fetch_active()
    if not sessions:
        print("no active sessions")
        return
    for s in sessions:
        print(
            f"{s.get('cpid')} {s.get('connectorId')} {s.get('idTag')} {s.get('transactionId')}"
        )


if __name__ == "__main__":
    main()