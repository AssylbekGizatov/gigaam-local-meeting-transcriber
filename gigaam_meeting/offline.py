"""Process-local network guard. Never changes Windows network/security settings."""
import os
import socket
import threading

_installed = False
_attempts = 0
_lock = threading.Lock()


def blocked_attempts():
    with _lock:
        return _attempts


def _deny(*args, **kwargs):
    global _attempts
    with _lock:
        _attempts += 1
    raise OSError("Network disabled in this offline application")


def install():
    global _installed
    if _installed:
        return
    for name in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "HF_HUB_DISABLE_TELEMETRY", "DO_NOT_TRACK"):
        os.environ[name] = "1"
    # No endpoint/audio/text is recorded. This is an application guard, not a
    # firewall: native DLLs could bypass Python socket APIs. See PRIVACY.md.
    socket.socket.connect = _deny
    socket.socket.connect_ex = _deny
    socket.socket.sendto = _deny
    # Do not invent POSIX-only methods on Windows: asyncio uses their presence
    # to choose os.sysconf paths that do not exist on Windows.
    if hasattr(socket.socket, "sendmsg"):
        socket.socket.sendmsg = _deny
    socket.create_connection = _deny
    socket.getaddrinfo = _deny
    _installed = True
