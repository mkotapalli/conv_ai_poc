from __future__ import annotations

import multiprocessing
import os
import signal
import sys
from collections.abc import Callable

import uvicorn


def _run_server(app_target: str, host: str, port: int) -> None:
    uvicorn.run(
        app_target, 
        host=host, 
        port=port, 
        log_level="info",
        access_log=True,  # Better for debugging
        reload=False      # Disable in production
    )


def _spawn(name: str, target: Callable[[], None]) -> multiprocessing.Process:
    process = multiprocessing.Process(target=target, name=name)
    process.start()
    return process


def signal_handler(signum, frame):
    """Handle shutdown signals gracefully"""
    print(f"Received signal {signum}, shutting down...")
    sys.exit(0)


if __name__ == "__main__":
    # Register signal handlers
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)
    
    rest_host = os.getenv("SERVER_HOST", "0.0.0.0")
    rest_port = int(os.getenv("SERVER_PORT", "8080"))
    mcp_host = os.getenv("MCP_SERVER_HOST", "0.0.0.0")
    mcp_port = int(os.getenv("MCP_SERVER_PORT", "8000"))

    print(f"Starting REST server on {rest_host}:{rest_port}")
    print(f"Starting MCP server on {mcp_host}:{mcp_port}")

    rest_process = _spawn("acct-mgmt-rest", lambda: _run_server("main:app", rest_host, rest_port))
    mcp_process = _spawn("acct-mgmt-mcp", lambda: _run_server("mcp_main:app", mcp_host, mcp_port))

    try:
        # Wait for both processes
        rest_process.join()
        mcp_process.join()
    except KeyboardInterrupt:
        print("Shutting down servers...")
    finally:
        for process in (rest_process, mcp_process):
            if process.is_alive():
                print(f"Terminating {process.name}")
                process.terminate()
                process.join(timeout=5)
                if process.is_alive():
                    print(f"Force killing {process.name}")
                    process.kill()
