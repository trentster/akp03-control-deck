from io import BytesIO
import stat

import pytest
from PIL import Image

from akp03.config import ConfigError, load_config
from akp03.daemon import Daemon, Paint, _label_image
from akp03.images import to_jpeg
from akp03.gui import (
    ActionDefinition,
    ControlPlaneEditorError,
    action_registry_toml,
    load_control_plane,
    replace_control_plane_sections,
    save_control_plane_config,
    validate_action_definitions,
    validate_tile_bindings,
)
from akp03.starter_sets import GENERAL_PURPOSE_SET, STARTER_SETS, StarterSet, StarterTile, artwork_gallery_categories


def _actions():
    return [
        ActionDefinition("copy", "keys", ("ctrl", "c")),
        ActionDefinition("dim", "brightness", brightness=25),
    ]


def test_action_registry_rendering_is_safe_and_never_enables_menu_or_feedback():
    rendered = action_registry_toml(_actions(), ["copy", "dim", None, "copy", None, None])

    assert '[actions.copy]' in rendered
    assert 'keys = ["ctrl", "c"]' in rendered
    assert '[actions.dim]' in rendered
    assert 'brightness = 25' in rendered
    assert '[bindings.key0]\naction_id = "copy"' in rendered
    assert "[menu]" not in rendered
    assert "[feedback]" not in rendered
    assert "shell" not in rendered
    assert "http" not in rendered
    assert "keys_hold" not in rendered


@pytest.mark.parametrize("definition, message", [
    (ActionDefinition("Bad", "keys", ("x",)), "action IDs"),
    (ActionDefinition("shell", "shell"), "only keys and brightness"),
    (ActionDefinition("hold", "keys_hold"), "only keys and brightness"),
    (ActionDefinition("bad-keys", "keys"), "non-empty"),
    (ActionDefinition("bad-brightness", "brightness", brightness=0), "1-100"),
])
def test_action_validation_rejects_unsafe_or_malformed_definitions(definition, message):
    with pytest.raises(ControlPlaneEditorError, match=message):
        validate_action_definitions([definition])


def test_tile_binding_validation_accepts_only_six_safe_action_references():
    validate_tile_bindings(["copy", None, "dim", None, None, None], _actions())
    with pytest.raises(ControlPlaneEditorError, match="exactly six"):
        validate_tile_bindings(["copy"], _actions())
    with pytest.raises(ControlPlaneEditorError, match="safe action"):
        validate_tile_bindings(["copy", None, "shell", None, None, None], _actions())


def test_tile_binding_validation_rejects_action_ids_too_long_for_lcd_label():
    action_id = "a" * 33
    actions = [ActionDefinition(action_id, "brightness", brightness=25)]
    with pytest.raises(ControlPlaneEditorError, match="at most 32 characters"):
        validate_tile_bindings([action_id, None, None, None, None, None], actions)


def test_section_replacement_retains_unrelated_and_unsafe_tables():
    existing = '''[actions.copy]
action = "keys"
keys = ["ctrl", "x"]

[actions.notify]
action = "shell"
shell = ["/usr/bin/notify-send", "kept"]

[bindings.key0]
action_id = "copy"

[bindings.top]
action_id = "notify"

[menu]
enabled = false

[feedback]
enabled = false
'''
    rendered = action_registry_toml(_actions(), ["copy", "dim", None, None, None, None])

    replaced = replace_control_plane_sections(existing, rendered, {"copy", "dim"})

    assert 'keys = ["ctrl", "x"]' not in replaced
    assert '[actions.notify]\naction = "shell"' in replaced
    assert '[bindings.top]\naction_id = "notify"' in replaced
    assert '[menu]\nenabled = false' in replaced
    assert '[feedback]\nenabled = false' in replaced
    assert replaced.count('[bindings.key0]') == 1


def test_safe_save_is_atomic_validated_preserves_0600_and_loads_existing_safe_actions(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('''[actions.paste]
action = "keys"
keys = ["ctrl", "v"]

[actions.external]
action = "http"
url = "https://example.invalid/hook"

[bindings.top]
action_id = "external"

[menu]
enabled = false

[feedback]
enabled = false
''')
    path.chmod(0o600)

    actions, bindings = load_control_plane(path)
    assert actions == [ActionDefinition("paste", "keys", ("ctrl", "v"))]
    assert bindings == [None, None, None, None, None, None]

    save_control_plane_config(path, _actions(), ["copy", "dim", None, None, None, None])

    saved = path.read_text()
    assert '[actions.external]\naction = "http"' in saved
    assert '[bindings.top]\naction_id = "external"' in saved
    assert '[menu]\nenabled = false' in saved
    assert '[feedback]\nenabled = false' in saved
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert load_control_plane(path) == (_actions(), ["copy", "dim", None, None, None, None])


def test_save_syncs_assigned_labels_preserves_unassigned_labels_and_daemon_renders_them(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('''[labels]
key0 = "Old assigned label"
key2 = "Keep this label"
key5 = "Also keep"

[feedback]
enabled = false
''')
    path.chmod(0o600)

    save_control_plane_config(path, _actions(), ["copy", "dim", None, None, None, None])

    loaded = load_config(path)
    assert loaded.labels == {0: "copy", 1: "dim", 2: "Keep this label", 5: "Also keep"}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600

    class Deck:
        def __init__(self): self.images = []
        def on_event(self, callback): self.event = callback
        def on_disconnect(self, callback): self.disconnect = callback
        def open(self): return self
        def close(self): pass
        def clear(self, key=None): pass
        def set_key_image(self, key, image): self.images.append((key, image.copy()))
        def set_brightness(self, level): pass

    deck = Deck()
    daemon = Daemon(path, state_path=tmp_path / "state.json", deck_factory=lambda _: deck)  # type: ignore[arg-type]
    assert daemon.reload() and daemon.connect()
    # The on-screen text is derived from the binding, not the static [labels]
    # entry: key1 is bound to the brightness action "dim", so it shows the same
    # text the editor presents for that assignment.
    assert daemon.labels[1].text == "Brightness 25%"
    assert daemon.labels[0].text == "Copy"
    rendered = dict(deck.images)
    assert rendered[1].tobytes() == _label_image(Paint(1, "Brightness 25%")).tobytes()


@pytest.mark.parametrize("text", ["", "x" * 33])
def test_config_rejects_labels_outside_lcd_text_limit(tmp_path, text):
    path = tmp_path / "config.toml"
    path.write_text(f'[labels]\nkey0 = "{text}"\n')
    path.chmod(0o600)
    with pytest.raises(ConfigError, match="1 through 32"):
        load_config(path)


def test_custom_image_save_is_managed_and_daemon_feedback_restores_art(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("[feedback]\nenabled = true\nduration_ms = 400\n")
    path.chmod(0o600)
    source = tmp_path / "source.png"
    Image.new("RGB", (120, 60), "red").save(source)

    save_control_plane_config(path, _actions(), ["copy", None, None, None, None, None], image_updates={0: source})

    loaded = load_config(path)
    asset = tmp_path / "images" / "key0.jpg"
    assert loaded.images == {0: asset}
    assert 'key0 = "key0.jpg"' in path.read_text() and str(source) not in path.read_text()
    assert asset.is_file() and stat.S_IMODE(asset.stat().st_mode) == 0o600
    assert stat.S_IMODE(asset.parent.stat().st_mode) == 0o700

    class Deck:
        def __init__(self): self.images = []
        def on_event(self, callback): self.event = callback
        def on_disconnect(self, callback): self.disconnect = callback
        def open(self): return self
        def close(self): pass
        def clear(self, key=None): pass
        def set_key_image(self, key, image): self.images.append((key, image.copy()))
        def set_brightness(self, level): pass

    class Timer:
        def __init__(self, delay, callback): self.delay, self.callback = delay, callback
        def start(self): pass
        def cancel(self): pass

    deck = Deck(); timers = []
    daemon = Daemon(path, state_path=tmp_path / "state", deck_factory=lambda _: deck, timer_factory=lambda delay, callback: timers.append(Timer(delay, callback)) or timers[-1])  # type: ignore[arg-type]
    assert daemon.reload() and daemon.connect()
    base = dict(deck.images)[0].tobytes()
    daemon.event(type("Event", (), {"kind": "key", "control": "key0", "pressed": True})())
    assert deck.images[-1][1].tobytes() != base
    timers[-1].callback()
    assert deck.images[-1][1].tobytes() == base

    save_control_plane_config(path, _actions(), ["copy", None, None, None, None, None], image_updates={0: None})
    assert load_config(path).images == {} and not asset.exists()


def test_starter_catalogue_is_a_single_coherent_inert_general_purpose_set():
    assert STARTER_SETS == (GENERAL_PURPOSE_SET,)
    assert GENERAL_PURPOSE_SET.stable_id == "general-purpose"
    assert [tile.stable_id for tile in GENERAL_PURPOSE_SET.tiles] == ["dim", "bright", "copy", "paste", "voice", "undo"]
    assert all(not tile.future_action_templates for tile in GENERAL_PURPOSE_SET.tiles)


def test_artwork_gallery_categories_are_derived_entirely_from_starter_sets():
    categories = artwork_gallery_categories()
    assert [(category.stable_id, category.display_name, category.tiles) for category in categories] == [
        (GENERAL_PURPOSE_SET.stable_id, GENERAL_PURPOSE_SET.display_name, GENERAL_PURPOSE_SET.tiles),
    ]

    future_set = StarterSet(
        "maker-cad", "Maker / CAD", "Future artwork for workshop tools.",
        (StarterTile("measure", "Measure", "Measure a selected feature.", "assets/starter-artwork/01-dim.png"),),
    )
    future_category = artwork_gallery_categories((future_set,))[0]
    assert future_category.stable_id == "maker-cad"
    assert future_category.display_name == "Maker / CAD"
    assert future_category.tiles[0].display_name == "Measure"


@pytest.mark.parametrize("images, message", [
    ('[images]\nkey0 = "else.jpg"\n', "exactly"),
    ('[images]\nkey6 = "key6.jpg"\n', "exactly"),
])
def test_config_rejects_non_managed_image_mapping(tmp_path, images, message):
    path = tmp_path / "config.toml"
    path.write_text(images)
    path.chmod(0o600)
    with pytest.raises(ConfigError, match=message):
        load_config(path)


def test_config_rejects_managed_image_symlink(tmp_path):
    path = tmp_path / "config.toml"
    assets = tmp_path / "images"; assets.mkdir()
    target = tmp_path / "outside.jpg"; target.write_bytes(b"not an image")
    (assets / "key0.jpg").symlink_to(target)
    path.write_text('[images]\nkey0 = "key0.jpg"\n')
    path.chmod(0o600)
    with pytest.raises(ConfigError, match="not a link"):
        load_config(path)


def test_safe_save_refuses_insecure_existing_mode(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("")
    path.chmod(0o644)

    with pytest.raises(ControlPlaneEditorError, match="0600"):
        save_control_plane_config(path, _actions(), [None] * 6)


def test_safe_save_refuses_to_overwrite_non_editable_action(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('''[actions.run]
action = "shell"
shell = ["/bin/true"]
''')
    path.chmod(0o600)

    with pytest.raises(ControlPlaneEditorError, match="non-editable"):
        save_control_plane_config(path, [ActionDefinition("run", "keys", ("ctrl", "r"))], [None] * 6)
    assert 'action = "shell"' in path.read_text()


def test_safe_save_refuses_to_overwrite_an_opaque_lcd_binding(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('''[bindings.key0]
action = "shell"
shell = ["/bin/true"]
''')
    path.chmod(0o600)

    with pytest.raises(ControlPlaneEditorError, match="will not be overwritten"):
        save_control_plane_config(path, _actions(), ["copy", None, None, None, None, None])
    assert 'action = "shell"' in path.read_text()


def test_safe_save_refuses_to_break_existing_references(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('''[actions.copy]
action = "keys"
keys = ["ctrl", "c"]
[bindings.top]
action_id = "copy"
''')
    path.chmod(0o600)

    with pytest.raises(ControlPlaneEditorError, match="configuration rejected"):
        save_control_plane_config(path, [], [None] * 6)
    assert '[actions.copy]' in path.read_text()
