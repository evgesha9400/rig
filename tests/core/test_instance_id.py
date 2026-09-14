"""Tests for stable, symlink-resolving instance identifiers."""

from rig import cli as rig

stack = rig


def test_instance_id_is_stable_for_the_same_checkout(tmp_path):
    first = stack.instance_id("deltalytic", tmp_path)
    second = stack.instance_id("deltalytic", tmp_path)
    assert first == second


def test_instance_id_differs_between_checkouts_of_the_same_project(tmp_path):
    left = tmp_path / "checkout-a"
    right = tmp_path / "checkout-b"
    left.mkdir()
    right.mkdir()

    assert stack.instance_id("deltalytic", left) != stack.instance_id("deltalytic", right)


def test_instance_id_uses_project_prefix_and_eight_hex_digits(tmp_path):
    ident = stack.instance_id("deltalytic", tmp_path)
    project, _, digest = ident.rpartition("-")

    assert project == "deltalytic"
    assert len(digest) == 8
    assert all(char in "0123456789abcdef" for char in digest)


def test_instance_id_resolves_symlinked_checkouts_to_one_identity(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)

    assert stack.instance_id("deltalytic", link) == stack.instance_id("deltalytic", real)


def test_instance_id_is_a_valid_compose_project_name(tmp_path):
    ident = stack.instance_id("Deltalytic_Pilot", tmp_path)

    assert ident == ident.lower()
    assert all(char.isalnum() or char in "-_" for char in ident)
    assert ident[0].isalnum()
