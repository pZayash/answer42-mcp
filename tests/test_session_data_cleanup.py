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


def _make_shared_dir(root, kind, digest, age_hours):
    base = root / "shared-file-servers" / kind / digest
    cache = base / "ibsrv-data" / "session-data"
    cache.mkdir(parents=True)
    (cache / "shard.dat").write_text("x" * 100)
    (base / "ibsrv-data" / "standalone.yaml").write_text("cfg")
    (base / "logs").mkdir()
    stamp = time.time() - age_hours * 3600
    for path in [base, *base.rglob("*")]:
        os.utime(path, (stamp, stamp))
    return base


def test_shared_server_folders_are_trimmed_removed_and_kept_when_running(data_root, monkeypatch):
    monkeypatch.setattr(server, "_SHARED_FILE_SERVERS_FILE", data_root / "shared-file-servers.json")
    monkeypatch.setattr(server, "_SHARED_FILE_SERVERS_LOCK", data_root / "shared-file-servers.lock")
    monkeypatch.setattr(server, "_SHARED_FILE_SERVERS", {})
    old = _make_shared_dir(data_root, "file", "old", age_hours=48)
    stopped = _make_shared_dir(data_root, "demo-client", "stopped", age_hours=3)
    running = _make_shared_dir(data_root, "demo-client", "running", age_hours=48)
    fresh = _make_shared_dir(data_root, "demo-client", "fresh", age_hours=0)
    server._save_shared_file_registry({"k": {"data_dir": str(running / "ibsrv-data"), "pid": 1, "port": 1}})
    monkeypatch.setattr(server, "_shared_file_entry_alive", lambda entry: True)

    result = server._sweep_shared_file_server_dirs(ttl_hours=24)

    assert result == {"removed": ["file/old"], "trimmed": ["demo-client/stopped"]}
    assert not old.exists()
    assert (stopped / "ibsrv-data" / "standalone.yaml").exists()
    assert not (stopped / "ibsrv-data" / "session-data").exists()
    assert (running / "ibsrv-data" / "session-data").exists()  # the server is running
    assert (fresh / "ibsrv-data" / "session-data").exists()  # grace period


def test_release_of_the_last_owner_drops_the_server_caches(data_root, monkeypatch):
    monkeypatch.setattr(server, "_SHARED_FILE_SERVERS_FILE", data_root / "shared-file-servers.json")
    monkeypatch.setattr(server, "_SHARED_FILE_SERVERS_LOCK", data_root / "shared-file-servers.lock")
    monkeypatch.setattr(server, "_SHARED_FILE_SERVERS", {})
    monkeypatch.setattr(server, "_kill_pid_group", lambda *_args, **_kwargs: None)
    base = _make_shared_dir(data_root, "demo-client", "one", age_hours=0)
    server._save_shared_file_registry({"k": {"data_dir": str(base / "ibsrv-data"), "pid": 1, "port": 1, "owners": ["s"]}})

    assert server._release_shared_file_server("k", "s") is True

    assert not (base / "ibsrv-data" / "session-data").exists()
    assert (base / "ibsrv-data" / "standalone.yaml").exists()


def test_unowned_service_folders(data_root):
    legacy = data_root / "html-inspector" / "8.5.1.1343" / "bin"
    legacy.mkdir(parents=True)
    (legacy / "1cv8.exe").write_text("x")
    build = data_root / "ib-build-cf"
    (build / "session-data").mkdir(parents=True)
    (build / "session-data" / "a.dat").write_text("x")
    (build / "db-data").mkdir()
    (build / "db-data" / "1Cv8.1CD").write_text("db")
    root_legacy = data_root / "html-inspector"
    stamp = time.time() - 3 * 3600
    for path in [root_legacy, build, *root_legacy.rglob("*"), *build.rglob("*")]:
        os.utime(path, (stamp, stamp))

    assert server._sweep_unowned_data_dirs(ttl_hours=24) == {"removed": [], "trimmed": ["ib-build-cf"]}
    assert not (build / "session-data").exists() and (build / "db-data" / "1Cv8.1CD").exists()
    old = time.time() - 48 * 3600
    for path in [root_legacy, build, *root_legacy.rglob("*"), *build.rglob("*")]:
        os.utime(path, (old, old))
    assert server._sweep_unowned_data_dirs(ttl_hours=24)["removed"] == ["ib-build-cf", "html-inspector"]
