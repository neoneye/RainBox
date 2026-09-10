"""The service catalogue's settings: generated registry entries and their
types, defaults, and internal flags (docs/superpowers/specs/2026-09-10-launcher-design.md)."""
import db
from services.definitions import STATIC_SERVICES, enabled_setting_key, env_setting_key, nonce_setting_key


def test_every_static_service_has_a_toggle_and_an_internal_nonce():
    for key, svc in STATIC_SERVICES.items():
        toggle = db.SETTINGS[enabled_setting_key(key)]
        assert toggle.type == "bool" and toggle.default is False and not toggle.internal
        nonce = db.SETTINGS[nonce_setting_key(key)]
        assert nonce.type == "string" and nonce.internal
        for var in svc.env_keys:
            spec = db.SETTINGS[env_setting_key(key, var)]
            assert spec.type == svc.env_spec(var).type and spec.default is None and not spec.internal


def test_core_has_only_a_nonce():
    assert db.SETTINGS[nonce_setting_key("core")].internal
    assert enabled_setting_key("core") not in db.SETTINGS


def test_catalogue_is_consistent_with_entrypoints():
    """Every kind's directory and script exist and the bind matches the port
    hard-coded in its server.py."""
    import pathlib, re
    root = pathlib.Path(__file__).resolve().parents[1]
    for svc in STATIC_SERVICES.values():
        script = root / svc.directory / svc.argv[-1]
        assert script.exists(), script
        port = svc.bind.rsplit(":", 1)[1]
        assert re.search(rf"port={port}\b", script.read_text()), (svc.key, port)


def test_env_specs_name_every_env_key_and_type_them():
    for svc in STATIC_SERVICES.values():
        assert {e.name for e in svc.env_specs} <= set(svc.env_keys)
        for var in svc.env_keys:
            spec = db.SETTINGS[env_setting_key(svc.key, var)]
            typed = svc.env_spec(var)
            assert spec.type == typed.type
            if typed.type == "int" and typed.positive:
                assert spec.validate is not None
            if typed.choices:
                assert spec.choices == typed.choices
