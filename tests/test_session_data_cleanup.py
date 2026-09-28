"""Session data folders: stopped sessions drop ibsrv caches, abandoned ones are swept."""

import os
import time

import pytest

from mcp_1c import os_support, server


def _make_session_dir(root, name, age_hours):
    data_dir = root / "sessions" / name
    (data_dir / "ib").mkdir(parents=True)
    (data_dir / "ib" / "1Cv8.1CD").write_text("db")
    cache = data_dir / "ibsrv-data" / "session-data"
    cache.mkdir(parents=True)
    (cache / "snccntx.dat").write_text("x" * 100)
    (data_dir / "ibsrv-data" / "standalone.yaml").write_text("cfg")
    stamp = time.time() - age_hours * 3600
    for path in [data_dir, *data_dir.rglob("*")]:
        os.utime(path, (stamp, stamp))
    return data_dir


@pytest.fixture
def data_root(tmp_path, monkeypatch):
    monkeypatch.setattr(os_support, "default_data_dir", lambda: tmp_path)
    monkeypatch.setattr(server, "GLOBAL_REGISTRY_FILE", tmp_path / "global-sessions.json")
    monkeypatch.setattr(server, "GLOBAL_REGISTRY_LOCK", tmp_path / "global-sessions.lock")
    monkeypatch.setattr(server, "_SESSIONS", {})
    return tmp_path


def test_sweep_removes_old_trims_recent_and_keeps_live(data_root):
    old = _make_session_dir(data_root, "old", age_hours=48)
    recent = _make_session_dir(data_root, "recent", age_hours=3)
    starting = _make_session_dir(data_root, "starting", age_hours=0)
    live = _make_session_dir(data_root, "live", age_hours=48)
    server._write_global_registry_unlocked({"sessions": {"x:live": {"data_dir": str(live), "status": "active"}}})

    result = server._sweep_session_data_dirs(ttl_hours=24)

    assert result == {"removed": ["old"], "trimmed": ["recent"]}
    assert not old.exists()
    assert (recent / "ib" / "1Cv8.1CD").exists() and (recent / "ibsrv-data" / "standalone.yaml").exists()
    assert not (recent / "ibsrv-data" / "session-data").exists()
    assert (starting / "ibsrv-data" / "session-data").exists()  # grace period: may be starting elsewhere
    assert (live / "ibsrv-data" / "session-data").exists()


def test_zero_ttl_only_trims(data_root):
    old = _make_session_dir(data_root, "old", age_hours=500)
    assert server._sweep_session_data_dirs(ttl_hours=0) == {"removed": [], "trimmed": ["old"]}
    assert (old / "ib").exists()


def test_trim_keeps_manager_infobase(tmp_path):
    data_dir = _make_session_dir(tmp_path, "s", age_hours=0)
    (data_dir / "ibsrv-data-client" / "temp").mkdir(parents=True)
    server._trim_session_data_dir(data_dir)
    assert (data_dir / "ib" / "1Cv8.1CD").exists()
    assert not (data_dir / "ibsrv-data" / "session-data").exists()
    assert not (data_dir / "ibsrv-data-client" / "temp").exists()
