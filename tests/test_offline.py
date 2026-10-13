import subprocess
import sys
from pathlib import Path


def test_guard_is_offline_and_windows_asyncio_safe():
    result = subprocess.run([sys.executable, "-c", """
from gigaam_meeting.offline import install, blocked_attempts
install()
import asyncio, socket
for call in (lambda: socket.create_connection(('127.0.0.1', 9)),
             lambda: socket.getaddrinfo('example.invalid', 443),
             lambda: socket.socket().connect(('127.0.0.1',9))):
    try:
        call()
        raise AssertionError('network guard failed')
    except OSError:
        pass
assert blocked_attempts() == 3
"""], cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
