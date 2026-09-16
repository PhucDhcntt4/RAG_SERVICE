"""Docker entrypoint and readiness probe; credentials stay in the mounted .env."""
import json
import sys
from urllib.request import Request, urlopen

from app.config import Settings


def healthcheck():
    try:
        settings = Settings.load()
        request = Request("http://127.0.0.1:8000/api/v1/health/ready", headers={
            "Authorization": "Bearer " + settings.search_api_key.get_secret_value(),
        })
        with urlopen(request, timeout=8) as response:
            return 0 if response.status == 200 and json.load(response).get("status") == "ready" else 1
    except Exception:
        # Docker retains probe output: never emit DSNs, keys or provider responses.
        print("RAG readiness check failed")
        return 1


def main():
    args = sys.argv[1:]
    if args == ["--healthcheck"]:
        raise SystemExit(healthcheck())
    if not args:
        raise SystemExit("Missing container command")
    try:
        Settings.load()
    except Exception:
        raise SystemExit("Invalid RAG configuration. Check the mounted .env file.") from None
    import os
    os.execvp(args[0], args)


if __name__ == "__main__":
    main()
