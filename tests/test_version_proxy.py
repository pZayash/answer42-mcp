import socket
import threading

from mcp_1c import platform as onec_platform
from mcp_1c.version_proxy import VersionProxy


def _echo_once(received: list[bytes]) -> socket.socket:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)

    def run() -> None:
        conn, _ = listener.accept()
        data = conn.recv(1024)
        received.append(data)
        conn.sendall(b'{"S","8.5.1.1343"} answer')
        conn.close()

    threading.Thread(target=run, daemon=True).start()
    return listener


def test_proxy_rewrites_the_version_in_both_directions():
    received: list[bytes] = []
    client = _echo_once(received)
    proxy = VersionProxy("127.0.0.1", client.getsockname()[1], "8.5.4.1878", "8.5.1.1343")
    try:
        port = proxy.start()
        with socket.create_connection(("127.0.0.1", port), timeout=5) as manager:
            manager.sendall(b'{"S","8.5.4.1878"} hello')
            reply = manager.recv(1024)
    finally:
        proxy.stop()
        client.close()
    assert received == [b'{"S","8.5.1.1343"} hello']  # the client sees its own version
    assert reply == b'{"S","8.5.4.1878"} answer'  # the manager sees its own version


def test_thin_client_dir_does_not_need_ibcmd(tmp_path, monkeypatch):
    full, thin = tmp_path / "8.5.4.1878" / "bin", tmp_path / "8.5.1.1343" / "bin"
    for path, tools in ((full, ("1cv8c", "ibcmd")), (thin, ("1cv8c",))):
        path.mkdir(parents=True)
        for tool in tools:
            (path / onec_platform.os_support.executable_name(tool)).write_text("")
    monkeypatch.setenv("ProgramFiles", str(tmp_path.parent / "none"))
    monkeypatch.setattr(
        onec_platform, "installed_platform_dirs",
        lambda tools=("1cv8c", "ibcmd"): [p for p in (full, thin) if not onec_platform._missing_required_tools(p, tools)],
    )
    assert onec_platform.find_thin_client_dir("8.5.1.1343") == thin
    assert onec_platform.find_platform_dir("8.5.4.1878") == full
