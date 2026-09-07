"""Docker entrypoint and readiness probe; credentials stay in the mounted .env."""
import json
import os
import sys
from urllib.request import Request, urlopen

from psycopg.conninfo import conninfo_to_dict, make_conninfo

from app.config import Settings


def database_url_for_container(database_url, host_override):
    """Keep remote DB hosts unchanged; only translate the Windows host's loopback."""
    if not host_override:
        return database_url
    parameters = conninfo_to_dict(database_url)
    if parameters.get("host") not in {"127.0.0.1", "localhost", "::1"}:
        return database_url
    parameters["host"] = host_override
    parameters.pop("hostaddr", None)
    return make_conninfo(**parameters)


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
        settings = Settings.load()
        os.environ["DATABASE_URL"] = database_url_for_container(
            settings.database_url.get_secret_value(), os.getenv("RAG_DATABASE_HOST_OVERRIDE", ""),
        )
    except Exception:
        raise SystemExit("Invalid RAG configuration. Check the mounted .env file.") from None
    os.execvp(args[0], args)


if __name__ == "__main__":
    main()
