"""Test process DNS only: simulated Railway runtime names resolve to loopback."""
import os
import socket

if os.environ.get("SQUIRE_PILOT_FIXTURE") == "1":
    original = socket.getaddrinfo

    def resolve(host, *args, **kwargs):
        if isinstance(host, str) and host.endswith(".pilot.test"):
            host = "127.0.0.1"
        return original(host, *args, **kwargs)

    socket.getaddrinfo = resolve
