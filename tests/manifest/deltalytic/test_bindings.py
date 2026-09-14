"""Manifest matching the deltalytic repository layout; backend socket transfer."""

from pathlib import Path

from rig import cli as rig

stack = rig

REPO_ROOT = Path(__file__).resolve().parents[3]


def test_repository_manifest_matches_the_repository_layout():
    manifest = stack.load_manifest(REPO_ROOT / "examples" / "stack.json")

    assert manifest.project == "deltalytic"
    assert set(manifest.services) >= {"backend", "frontend"}
    assert manifest.resolve_scope("full")
    for scope in ("full", "local", "backend", "ui"):
        assert manifest.resolve_scope(scope)


def test_repository_manifest_uses_the_real_health_endpoint():
    manifest = stack.load_manifest(REPO_ROOT / "examples" / "stack.json")

    assert manifest.services["backend"].healthcheck_path == "/api/v1/health"


def test_repository_manifest_transfers_a_socket_to_the_backend():
    manifest = stack.load_manifest(REPO_ROOT / "examples" / "stack.json")
    backend = manifest.services["backend"]

    assert backend.type == "fd"
    assert "{fd}" in backend.command
    assert "--fd" in backend.command


def test_repository_manifest_points_the_backend_at_the_project_interpreter():
    manifest = stack.load_manifest(REPO_ROOT / "examples" / "stack.json")
    backend = manifest.services["backend"]
    assert "--fd" in backend.command
    assert "{fd}" in backend.command


def test_repository_manifest_frontend_is_the_process_that_actually_serves():
    manifest = stack.load_manifest(REPO_ROOT / "examples" / "stack.json")
    command = manifest.services["frontend"].command

    assert "npm" not in command
    assert command[0] == "node"
    assert "--strictPort" in command
    assert "{port}" in command


def test_repository_manifest_hands_the_backend_port_to_the_frontend():
    manifest = stack.load_manifest(REPO_ROOT / "examples" / "stack.json")
    frontend = manifest.services["frontend"]

    assert "backend" in frontend.depends_on
    assert frontend.env.get("BACKEND_PORT") == "{backend_port}"


def test_repository_manifest_keeps_the_backend_on_sqlite():
    manifest = stack.load_manifest(REPO_ROOT / "examples" / "stack.json")
    backend = manifest.services["backend"]

    assert backend.env["DATABASE_URL"].startswith("sqlite:")
    assert not any(service.type == "compose" for service in manifest.services.values())
