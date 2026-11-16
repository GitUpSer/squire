"""Run the upstream CLI (including migrations) with an explicit dual-stack socket."""
import os
import socket
import sys


def dualstack_run(app, **kwargs):
    import uvicorn

    if kwargs.get("workers", 1) != 1 or kwargs.get("reload", False):
        raise RuntimeError("The pilot proxy requires one worker and no reload")
    port = int(kwargs.get("port", os.environ.get("PORT", "4000")))
    with socket.create_server(
        ("::", port), family=socket.AF_INET6, dualstack_ipv6=True
    ) as listener:
        uvicorn.Server(uvicorn.Config(app, **kwargs)).run(sockets=[listener])


def main():
    import uvicorn
    from litellm.proxy.proxy_cli import run_server

    # Keep LiteLLM's configuration, Prisma migrations and signal handling.
    # Binding host='::' through asyncio alone enables IPV6_V6ONLY on Linux.
    uvicorn.run = dualstack_run
    run_server([
        "--config", "/app/config.yaml", "--host", "::",
        "--port", os.environ.get("PORT", "4000"), "--num_workers", "1",
        *sys.argv[1:],
    ])


if __name__ == "__main__":
    main()
