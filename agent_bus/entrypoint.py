from __future__ import annotations

import click

from agent_bus.cli import cli as cli_group
from agent_bus.version import __version__


@click.group(invoke_without_command=True)
@click.version_option(__version__, prog_name="agent-bus", message="%(prog)s %(version)s")
@click.pass_context
def main(ctx: click.Context) -> None:
    """Agent Bus peer MCP server and administrative CLI."""
    if ctx.invoked_subcommand is None:
        from agent_bus.peer_server import main as server_main

        server_main()


@main.command("serve")
@click.option(
    "--host",
    default=None,
    help="Host to bind to (defaults to 0.0.0.0 when $PORT is set, else 127.0.0.1).",
)
@click.option(
    "--port",
    "-p",
    default=None,
    type=int,
    help="Port to bind to (defaults to $PORT or 8080).",
)
@click.option(
    "--db-path",
    default=None,
    help="SQLite DB path (defaults to $AGENT_BUS_DB or ~/.agent_bus/agent_bus.sqlite).",
)
def serve_command(host: str | None, port: int | None, db_path: str | None) -> None:
    """Start the Agent Bus Web UI and MCP transports (Streamable HTTP at /mcp, SSE at /sse)."""
    import os

    try:
        from agent_bus.web.server import run_server
    except ImportError:
        raise click.ClickException(
            "Web UI dependencies not installed. Install with: uv sync --extra web"
        ) from None

    from agent_bus.oauth import missing_env

    missing = missing_env()
    if missing:
        raise click.ClickException(
            "Refusing to start an unauthenticated HTTP server. Set: "
            + ", ".join(missing)
            + "\n(AGENT_BUS_OKTA_ISSUER/CLIENT_ID/CLIENT_SECRET + AGENT_BUS_PUBLIC_URL). "
            "See the Okta setup docs. stdio (`agent-bus`) needs no auth."
        )

    bind_host = (
        host
        or os.environ.get("AGENT_BUS_HOST")
        or ("0.0.0.0" if os.environ.get("PORT") else "127.0.0.1")
    )
    bind_port = port or (int(os.environ["PORT"]) if os.environ.get("PORT") else 8080)

    click.echo(f"Starting Agent Bus Web UI + MCP at http://{bind_host}:{bind_port}")
    click.echo("  MCP (Streamable HTTP): /mcp | MCP (SSE): /sse | Web UI: /")
    click.echo("Press Ctrl+C to stop.")

    from agent_bus.embedding_worker import start_background_embedding_worker

    if db_path:
        os.environ["AGENT_BUS_DB"] = db_path  # bind peer server DB before its import
    from agent_bus.peer_server import db as peer_db

    start_background_embedding_worker(peer_db)
    run_server(host=bind_host, port=bind_port, db_path=db_path)


main.add_command(cli_group, name="cli")

if __name__ == "__main__":
    main()
