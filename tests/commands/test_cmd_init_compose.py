"""cmd_init: compose-based database/cache detection and symlink safety under --force."""

import json
import os
import textwrap

from rig import cli as rig

stack = rig


def test_init_compose_detection_uses_service_names_and_images(tmp_path):
    """An application that merely mentions postgres is not a database."""
    proj = tmp_path / "app"
    proj.mkdir()
    (proj / "compose.yml").write_text(
        textwrap.dedent(
            """\
            services:
              web:
                build: .
                environment:
                  DATABASE_URL: postgres://user:pw@database:5432/app
                  REDIS_URL: redis://cache:6379/0
              database:
                image: postgres:16-alpine
                ports:
                  - "5432:5432"
              cache:
                image: valkey/valkey:8
            """
        )
    )
    assert stack.cmd_init(proj, dry_run=False) == stack.EXIT_OK
    data = json.loads((proj / "rig.json").read_text())
    services = data["services"]
    assert services["postgres"]["compose_service"] == "database"
    assert services["redis"]["compose_service"] == "cache"


def test_init_compose_detection_ignores_non_database_image(tmp_path):
    """A service named db running something else is not treated as postgres."""
    proj = tmp_path / "mysqlapp"
    proj.mkdir()
    (proj / "compose.yml").write_text(
        'services:\n  db:\n    image: mysql:8\n    ports:\n      - "3306:3306"\n'
    )
    assert stack.cmd_init(proj, dry_run=False) == stack.EXIT_OK
    data = json.loads((proj / "rig.json").read_text())
    assert "postgres" not in data.get("services", {})


def test_init_force_does_not_follow_predictable_temp_symlink(tmp_path):
    """`init --force` must not write through a planted temp symlink."""
    proj = tmp_path / "forced"
    proj.mkdir()
    (proj / "pyproject.toml").write_text("[project]\nname='forced'\ndependencies=['fastapi']\n")
    (proj / "rig.json").write_text("{}\n")
    victim = tmp_path / "victim.txt"
    victim.write_text("untouched")
    (proj / f".rig.json.tmp.{os.getpid()}").symlink_to(victim)

    assert stack.cmd_init(proj, force=True) == stack.EXIT_OK
    assert victim.read_text() == "untouched"
    assert json.loads((proj / "rig.json").read_text())["project"] == "forced"
