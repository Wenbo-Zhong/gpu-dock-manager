"""Run with dbus-run-session. Uses a fake controller, never the host system bus."""
from collections import deque
import os
import subprocess
import sys
import threading
import time


def server():
    from usb4_gpu_manager import daemon
    class FakeController:
        def __init__(self):
            self.events = deque()
            self.operation_lock = threading.Lock()
            self.cancel_event = threading.Event()
        def get_status(self):
            return {"service_connected": True, "test_bus": True, "can_mutate_devices": True, "events": list(self.events)}
        def event(self, message):
            self.events.append({"time": time.time(), "message": message})
        def tick(self):
            pass
    daemon.Controller = FakeController
    daemon.recent_events = lambda: []
    os.geteuid = lambda: 0
    instance = daemon.Service()
    instance.authorized = lambda *_: False
    instance.run()


def main():
    assert os.environ.get("DBUS_SESSION_BUS_ADDRESS"), "Use dbus-run-session"
    os.environ["DBUS_SYSTEM_BUS_ADDRESS"] = os.environ["DBUS_SESSION_BUS_ADDRESS"]
    if len(sys.argv) > 1 and sys.argv[1] == "server":
        server()
        return
    from usb4_gpu_manager.client import Client
    child = subprocess.Popen([sys.executable, "-m", "tests.dbus_smoke", "server"])
    try:
        client = Client()
        for _ in range(50):
            try:
                value = client.call("GetStatus")
                break
            except Exception:
                time.sleep(.1)
        else:
            raise AssertionError("Fake service did not start")
        assert value["test_bus"] and value["service_connected"]
        assert isinstance(client.call("GetLogs"), list)
        for action in ["detach", "deploy", "bogus"]:
            try:
                client.perform(action)
            except Exception:
                pass
            else:
                raise AssertionError("Denied action unexpectedly succeeded")
        print("PASS: isolated D-Bus status/logs, mutation authorization rejection, unknown action rejection")
    finally:
        child.terminate()
        child.wait(timeout=5)


if __name__ == "__main__":
    main()
