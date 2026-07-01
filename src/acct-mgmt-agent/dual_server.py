from __future__ import annotations

import multiprocessing
import os
from collections.abc import Callable

import uvicorn


def _run_server(app_target: str, host: str, port: int) -> None:
    uvicorn.run(app_target, host=host, port=port, log_level="info")


def _spawn(name: str, target: Callable[[], None]) -> multiprocessing.Process:
    process = multiprocessing.Process(target=target, name=name)
    process.start()
    return process


if __name__ == "__main__":
    rest_host = os.getenv("SERVER_HOST", "0.0.0.0")
    rest_port = int(os.getenv("SERVER_PORT", "8080"))
    mcp_host = os.getenv("MCP_SERVER_HOST", "0.0.0.0")
    mcp_port = int(os.getenv("MCP_SERVER_PORT", "8000"))

    rest_process = _spawn("acct-mgmt-rest", lambda: _run_server("main:app", rest_host, rest_port))
    mcp_process = _spawn("acct-mgmt-mcp", lambda: _run_server("mcp_main:app", mcp_host, mcp_port))

    try:
        rest_process.join()
        mcp_process.join()
    except KeyboardInterrupt:
        pass
    finally:
        for process in (rest_process, mcp_process):
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
