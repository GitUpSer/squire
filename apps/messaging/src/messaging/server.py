"""One explicit dual-stack listener for Railway IPv4 probes and private IPv6."""
import os
import socket
import uvicorn


def main():
    listener = socket.create_server(("::", int(os.environ.get("PORT", "8080"))),
                                    family=socket.AF_INET6, dualstack_ipv6=True)
    try:
        server = uvicorn.Server(uvicorn.Config("messaging.main:app", access_log=False))
        server.run(sockets=[listener])
    finally:
        listener.close()


if __name__ == "__main__":
    main()
