import os
import shutil
import subprocess

import pytest

# A bus that accepts every connection and every call, and starts no services on demand, so a test
# can't wake up a real program on it.
_PRIVATE_BUS_CONFIG = """<busconfig>
  <type>session</type>
  <listen>unix:path={socket}</listen>
  <auth>EXTERNAL</auth>
  <policy context="default">
    <allow send_destination="*" eavesdrop="true"/>
    <allow eavesdrop="true"/>
    <allow own="*"/>
  </policy>
</busconfig>
"""


@pytest.fixture(autouse=True, scope="session")
def no_session_bus(tmp_path_factory):
    """Point the session bus at a socket that doesn't exist, so no test reaches the user's own
    session bus by accident. Tests that need a bus use ``private_bus``."""
    previous = os.environ.get("DBUS_SESSION_BUS_ADDRESS")
    missing = tmp_path_factory.mktemp("no-bus") / "bus"
    os.environ["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={missing}"
    yield
    if previous is None:
        del os.environ["DBUS_SESSION_BUS_ADDRESS"]
    else:
        os.environ["DBUS_SESSION_BUS_ADDRESS"] = previous


@pytest.fixture
def private_bus(tmp_path):
    """Run a private ``dbus-daemon`` for one test and return its address."""
    daemon = shutil.which("dbus-daemon")
    if daemon is None:
        pytest.skip("dbus-daemon is not installed")
    config = tmp_path / "bus.conf"
    socket = tmp_path / "bus"
    # A Unix socket path has room for 107 bytes.
    assert len(str(socket)) < 100, f"socket path too long for a Unix socket: {socket}"
    config.write_text(_PRIVATE_BUS_CONFIG.format(socket=socket))
    process = subprocess.Popen(
        [daemon, f"--config-file={config}", "--nofork", "--nopidfile", "--print-address=1"],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout is not None
        address = process.stdout.readline().strip()
        assert address, "dbus-daemon printed no address"
        yield address
    finally:
        process.terminate()
        process.wait(timeout=5)
        if process.stdout is not None:
            process.stdout.close()
