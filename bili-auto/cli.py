"""CLI entry points for bili-auto."""
import asyncio


def main() -> None:
    """Execute one round of auto-interaction."""
    from .core import run_once
    asyncio.run(run_once())


if __name__ == "__main__":
    main()
