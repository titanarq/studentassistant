#!/usr/bin/env python3
"""Mints a GitHub App installation access token, so a worker commits and comments as its own
bot identity (`<slug>[bot]`) rather than the human account.

    python -m agent_os.gh_app_token --app your-repo-claude              # token to stdout
    python -m agent_os.gh_app_token --app your-repo-claude --bot-name   # "your-repo-claude[bot]"
    python -m agent_os.gh_app_token --app your-repo-claude --bot-email  # "<id>+your-repo-claude[bot]@users.noreply.github.com"
    python -m agent_os.gh_app_token --app your-repo-claude --who        # all three, one per line
    python -m agent_os.gh_app_token --app your-repo-claude --org my-org # only needed the first
                                                                              # time, to discover installation_id

Secrets live under `project.secrets_dir` (`.secrets/gh_apps` unless a project's own
`config/agents.yaml` names another directory), as `<slug>.json`, gitignored and never in this
repo's tracked files:

    {"app_id": 123456, "installation_id": 789, "private_key_path": ".secrets/gh_apps/<slug>.pem"}

`installation_id` may be omitted on first use: it is discovered via `GET /app/installations` (the
JWT alone can list them), matched against `--org`'s account login (or taken as the only one if
there is exactly one installation), and written back into the JSON so later runs skip the lookup.

The JWT (RS256, `iat` 60s in the past, `exp` 9 minutes out, `iss` = app id) authenticates as the
App itself; exchanging it at `POST /app/installations/{id}/access_tokens` yields the installation
token that actually acts on repositories. Both verified against the GitHub REST API docs via
Context7 (2026-09-13): "Generating a JSON Web Token (JWT) for a GitHub App" (the iat/exp/iss/RS256
claims) and `/rest/apps/apps` (`POST /app/installations/{installation_id}/access_tokens`, `GET
/app`, `GET /app/installations`). Installation tokens expire one hour from creation; the mint is
cached in `.cache/gh_app_token_<slug>.json` and reused while more than 5 minutes remain.

`--who` mints a token (needed to reach `GET /app` for the bot's numeric id) so it fails the same
way `--app <slug>` alone does when the secrets file is missing: one line on stderr, exit 2.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import NoReturn

import jwt

from agent_os.cli import host_root
from agent_os.lib import load_project

# The HOST project's root, resolved rather than assumed: `$AGENT_OS_HOST_ROOT`, else the git
# checkout the call is made from. Everything a project owns hangs off it -- `config/agents.yaml`,
# `.cache/`, `.secrets/`, `.github/ISSUE_TEMPLATE/`, the `project.worktrees` entries -- and none of
# it is derived from this package's own location, which is what made the mechanism able to run
# exactly one project (agent_os/docs/adr/2026-09-21-the-mechanism-is-one-directory-extended-by-hosts-and-
# never-modified.md).
HOST_ROOT = host_root()
# `project.secrets_dir` through the same config loader `agent_task.sh` reads it with
# (`agent_project_value --path secrets_dir`, itself `agent_os.lib`'s `project-value`), resolved
# against the host root exactly as that shell helper does -- a project that names a different
# directory needs no edit here, only its own `config/agents.yaml`.
SECRETS_DIR = HOST_ROOT / load_project().secrets_dir
CACHE_DIR = HOST_ROOT / ".cache"
API = "https://api.github.com"


def fail(message: str) -> NoReturn:
    print(f"gh_app_token: {message}", file=sys.stderr)
    sys.exit(2)


def load_secrets(slug: str) -> tuple[dict, Path]:
    path = SECRETS_DIR / f"{slug}.json"
    if not path.is_file():
        fail(f"no secrets file at {path} -- create it first (`project.secrets_dir` in config)")
    try:
        secrets = json.loads(path.read_text())
    except ValueError as exc:
        fail(f"{path} is not valid JSON: {exc}")
    for key in ("app_id", "private_key_path"):
        if key not in secrets:
            fail(f"{path} is missing required key '{key}'")
    return secrets, path


def private_key_path(secrets: dict) -> Path:
    raw = Path(secrets["private_key_path"])
    return raw if raw.is_absolute() else HOST_ROOT / raw


def build_app_jwt(secrets: dict) -> str:
    key_path = private_key_path(secrets)
    if not key_path.is_file():
        fail(f"private key not found at {key_path}")
    now = int(time.time())
    payload = {"iat": now - 60, "exp": now + 9 * 60, "iss": str(secrets["app_id"])}
    return jwt.encode(payload, key_path.read_bytes(), algorithm="RS256")


def api(method: str, path: str, token: str, body: dict | None = None) -> dict | list:
    request = urllib.request.Request(
        f"{API}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:300]
        fail(f"GitHub API {method} {path} -> {exc.code}: {detail}")
    except urllib.error.URLError as exc:
        fail(f"GitHub API {method} {path} unreachable: {exc.reason}")


def api_dict(method: str, path: str, token: str, body: dict | None = None) -> dict:
    """`api`, narrowed to the endpoints whose payload is a JSON object (everything here except
    `GET /app/installations`, which is a list -- see `api_list`)."""
    result = api(method, path, token, body)
    if not isinstance(result, dict):
        fail(f"GitHub API {method} {path} returned a {type(result).__name__}, expected an object")
    return result


def api_list(method: str, path: str, token: str) -> list:
    result = api(method, path, token)
    if not isinstance(result, list):
        fail(f"GitHub API {method} {path} returned a {type(result).__name__}, expected a list")
    return result


def discover_installation_id(
    app_jwt: str, org: str | None, secrets: dict, secrets_path: Path
) -> int:
    installations = api_list("GET", "/app/installations", app_jwt)
    if org:
        matches = [i for i in installations if (i.get("account") or {}).get("login") == org]
        if not matches:
            fail(
                f"no installation found for org '{org}' (installations seen: "
                f"{[((i.get('account') or {}).get('login')) for i in installations]})"
            )
    else:
        matches = installations
        if len(matches) != 1:
            fail(
                f"{len(matches)} installations found; pass --org to pick one "
                f"(seen: {[((i.get('account') or {}).get('login')) for i in installations]})"
            )
    installation_id = matches[0]["id"]
    secrets["installation_id"] = installation_id
    secrets_path.write_text(json.dumps(secrets, indent=2) + "\n")
    return installation_id


def cache_path(slug: str) -> Path:
    return CACHE_DIR / f"gh_app_token_{slug}.json"


def cached_token(slug: str) -> str | None:
    path = cache_path(slug)
    if not path.is_file():
        return None
    try:
        cached = json.loads(path.read_text())
        if cached["expires_at"] - time.time() > 300:
            return cached["token"]
    except (ValueError, KeyError):
        pass
    return None


def mint_token(slug: str, org: str | None) -> str:
    token = cached_token(slug)
    if token:
        return token
    secrets, secrets_path = load_secrets(slug)
    app_jwt = build_app_jwt(secrets)
    installation_id = secrets.get("installation_id") or discover_installation_id(
        app_jwt, org, secrets, secrets_path
    )
    result = api_dict("POST", f"/app/installations/{installation_id}/access_tokens", app_jwt)
    token = result["token"]
    expires_at = datetime.fromisoformat(result["expires_at"]).timestamp()
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path(slug).write_text(
        json.dumps({"token": token, "expires_at": expires_at}, indent=2) + "\n"
    )
    return token


def app_identity(slug: str) -> dict:
    """The App's own record (`id`, `slug`, ...) via GET /app, authenticated as the App (JWT).

    Org-independent by design: GET /app returns the App itself, not one of its installations, so
    unlike `discover_installation_id` this never needs `--org`.
    """
    secrets, _ = load_secrets(slug)
    app_jwt = build_app_jwt(secrets)
    return api_dict("GET", "/app", app_jwt)


def bot_name(slug: str) -> str:
    return f"{slug}[bot]"


def bot_email(slug: str) -> str:
    return f"{app_identity(slug)['id']}+{slug}[bot]@users.noreply.github.com"


def main() -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--app", required=True, help="App slug, e.g. your-repo-claude")
    parser.add_argument("--org", help="Org login to disambiguate GET /app/installations")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--bot-email", action="store_true")
    mode.add_argument("--bot-name", action="store_true")
    mode.add_argument(
        "--who", action="store_true", help="token + bot-name + bot-email, one per line"
    )
    args = parser.parse_args()

    if args.bot_name:
        print(bot_name(args.app))
    elif args.bot_email:
        print(bot_email(args.app))
    elif args.who:
        print(f"token       {mint_token(args.app, args.org)}")
        print(f"bot-name    {bot_name(args.app)}")
        print(f"bot-email   {bot_email(args.app)}")
    else:
        print(mint_token(args.app, args.org))


if __name__ == "__main__":
    main()
