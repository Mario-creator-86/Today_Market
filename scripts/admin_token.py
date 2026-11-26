"""Print the administrative token for the owner of this machine: python -m scripts.admin_token

Settings -> Data providers asks for ADMIN_TOKEN before provider settings can be viewed or changed. This prints the
value the server checks (the project .env, unless an OS environment variable overrides it) for the local user who
runs it; that user can already read .env. Do not run it where the output is recorded (CI logs, shared terminals)."""
import sys
from pathlib import Path

from backend import config
from backend.config import Settings


def admin_token(env_file: Path | None = None) -> str:
    settings = Settings(_env_file=env_file or config.ENV_FILE)
    return settings.admin_token.get_secret_value().strip()


def main() -> int:
    token = admin_token()
    if not token:
        print('ADMIN_TOKEN is not set. Run: python scripts/init_local.py', file=sys.stderr)
        return 1
    print(token)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
