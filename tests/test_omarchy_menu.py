from __future__ import annotations

from types import SimpleNamespace

import pytest

from akp03.config import Binding, ConfigError, load_config
from akp03.daemon import Daemon
from akp03.gui import ActionDefinition, ControlPlaneEditorError, action_registry_toml, load_control_plane, validate_action_definitions
from akp03.omarchy_menu_catalogue import ALLOWED_OMARCHY_MENU_ROUTES, OMARCHY_MENU_EXECUTABLE, OMARCHY_MENU_ROUTES


def config(path, text: str) -> None:
    path.write_text(text)
    path.chmod(0o600)


def test_catalogue_is_closed_and_contains_only_reviewed_navigation_routes():
    assert [entry.route for entry in OMARCHY_MENU_ROUTES] == [
        "root", "apps", "learn", "trigger", "style", "setup", "install", "remove", "update", "system",
    ]
    assert ALLOWED_OMARCHY_MENU_ROUTES == {entry.route for entry in OMARCHY_MENU_ROUTES}
    assert all("shutdown" not in entry.route and "suspend" not in entry.route for entry in OMARCHY_MENU_ROUTES)


def test_named_omarchy_menu_action_requires_a_reviewed_catalogue_route(tmp_path):
    path = tmp_path / "config.toml"
    config(path, '''[actions.go]
action = "omarchy_menu"
route = "root"
[bindings.key0]
action_id = "go"
''')

    loaded = load_config(path)
    assert loaded.actions["go"] == Binding(action="omarchy_menu", omarchy_route="root")
    assert loaded.bindings["key0"] is loaded.actions["go"]

    config(path, '''[actions.go]
action = "omarchy_menu"
route = "shutdown"
''')
    with pytest.raises(ConfigError, match="reviewed Omarchy"):
        load_config(path)

    config(path, '''[bindings.key0]
action = "omarchy_menu"
route = "root"
''')
    with pytest.raises(ConfigError, match="must use a named action_id"):
        load_config(path)


def test_gui_renders_and_reloads_only_catalogued_omarchy_menu_actions(tmp_path):
    actions = [ActionDefinition("omarchy-go", "omarchy_menu", omarchy_route="root")]
    rendered = action_registry_toml(actions, ["omarchy-go", None, None, None, None, None])
    assert 'action = "omarchy_menu"' in rendered
    assert 'route = "root"' in rendered
    assert "shell" not in rendered and "http" not in rendered

    path = tmp_path / "config.toml"
    config(path, rendered)
    assert load_control_plane(path) == (actions, ["omarchy-go", None, None, None, None, None])

    with pytest.raises(ControlPlaneEditorError, match="reviewed Omarchy"):
        validate_action_definitions([ActionDefinition("bad-route", "omarchy_menu", omarchy_route="shutdown")])


def test_daemon_launches_exact_fixed_omarchy_executable_without_shell(monkeypatch, tmp_path):
    calls = []

    def run(*args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("akp03.daemon.subprocess.run", run)
    daemon = Daemon(tmp_path / "config.toml")
    daemon._run_omarchy_menu(Binding(action="omarchy_menu", omarchy_route="style"))

    assert calls == [(
        ([OMARCHY_MENU_EXECUTABLE, "summon", "style"],),
        {"shell": False, "check": False, "timeout": 10, "capture_output": True, "text": True, "errors": "replace"},
    )]
