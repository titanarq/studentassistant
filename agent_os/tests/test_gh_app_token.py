"""`agent_os/agent_os/gh_app_token.py` reads its secrets directory from `project.secrets_dir`
through the same config loader `agent_task.sh` uses (`agent_project_value --path secrets_dir`,
`agent_os.lib`'s `project-value`) -- the hardcoded `.secrets/gh_apps` this module used to carry is
gone (#510).

`HOST_ROOT` and `SECRETS_DIR` are resolved once, at import time, from `$AGENT_OS_HOST_ROOT` and the
config it names -- exactly like `agent_os.lib`'s own `HOST_ROOT`/`DEFAULT_AGENTS_CONFIG` -- so the
only way to observe a different value is a fresh process with a different environment, the same
approach `test_role_run_environment_isolation.py` uses for the same reason.

Pure subprocess. This file must not request the `engine` or `db_sandbox` fixture.
"""

from __future__ import annotations

import os
import subprocess
import sys

from agent_os.cli import AGENT_OS_DIR

HOST_ROOT = AGENT_OS_DIR.parent


def _write_config(tmp_path, secrets_dir: str | None):
    """A minimal, valid `config/agents.yaml` under a throwaway host root -- `project.secrets_dir`
    set to `secrets_dir`, or left out entirely to exercise the field's own default."""
    secrets_line = f"  secrets_dir: {secrets_dir}\n" if secrets_dir is not None else ""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "agents.yaml").write_text(
        "project:\n"
        "  repo: someone/something\n"
        "  tracking_epic: 1\n"
        "  board_number: 1\n" + secrets_line + "classes: {}\n"
    )


def _run_gh_app_token(tmp_path, *args):
    environment = dict(os.environ)
    environment["AGENT_OS_HOST_ROOT"] = str(tmp_path)
    environment.pop("AGENTS_CONFIG_PATH", None)
    return subprocess.run(
        [sys.executable, "-m", "agent_os.gh_app_token", *args],
        capture_output=True,
        text=True,
        timeout=30,
        env=environment,
        check=False,
    )


def test_secrets_dir_reads_the_configured_directory_not_the_hardcoded_default(tmp_path):
    # A non-default directory (#510's own acceptance criterion): the refusal must name IT, never
    # the `.secrets/gh_apps` this module used to hardcode regardless of what the config said.
    _write_config(tmp_path, "config/gh_identities")
    result = _run_gh_app_token(tmp_path, "--app", "some-app")
    assert result.returncode == 2
    expected = tmp_path / "config" / "gh_identities" / "some-app.json"
    assert str(expected) in result.stderr
    assert ".secrets/gh_apps" not in result.stderr


def test_secrets_dir_defaults_to_dot_secrets_gh_apps_when_the_key_is_absent(tmp_path):
    # No existing config changes behaviour: a project that never set `project.secrets_dir` gets
    # exactly the directory this module used to hardcode.
    _write_config(tmp_path, None)
    result = _run_gh_app_token(tmp_path, "--app", "some-app")
    assert result.returncode == 2
    expected = tmp_path / ".secrets" / "gh_apps" / "some-app.json"
    assert str(expected) in result.stderr


def test_declared_dependencies_pull_in_pyjwts_crypto_extra():
    # #6: the App JWT is signed RS256, which PyJWT only registers when `cryptography` is present --
    # and a bare `PyJWT>=2.8` does not bring it. `bootstrap.sh` installs exactly what this file
    # declares, so without the `[crypto]` extra (or `cryptography` by name) the mechanism's own
    # interpreter could not sign the JWT: `KeyError: 'RS256'`.
    import tomllib

    from packaging.requirements import Requirement

    pyproject = tomllib.loads((AGENT_OS_DIR / "pyproject.toml").read_text())
    requirements = [Requirement(line) for line in pyproject["project"]["dependencies"]]
    names = {requirement.name.lower(): requirement for requirement in requirements}
    assert "cryptography" in names or "crypto" in names["pyjwt"].extras, (
        "PyJWT is declared without its [crypto] extra and cryptography is not declared either"
    )


def test_build_app_jwt_signs_rs256_with_the_declared_dependencies(tmp_path):
    # The same failure through the module's own code path: an RSA key on disk, `build_app_jwt` run
    # in a fresh interpreter, a JWT out -- not the `KeyError: 'RS256'` a crypto-less PyJWT raises.
    _write_config(tmp_path, None)
    key_path = tmp_path / "app.pem"
    # `openssl`, not `cryptography`, makes the key: the test must not need the very package whose
    # absence it is there to catch.
    subprocess.run(
        ["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048"]
        + ["-out", str(key_path)],
        capture_output=True,
        check=True,
        timeout=30,
    )
    environment = dict(os.environ)
    environment["AGENT_OS_HOST_ROOT"] = str(tmp_path)
    environment.pop("AGENTS_CONFIG_PATH", None)
    script = (
        "from agent_os.gh_app_token import build_app_jwt\n"
        f"print(build_app_jwt({{'app_id': 123, 'private_key_path': {str(key_path)!r}}}))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
        env=environment,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    import jwt

    token = result.stdout.strip()
    assert jwt.get_unverified_header(token)["alg"] == "RS256"
    assert jwt.decode(token, options={"verify_signature": False})["iss"] == "123"
