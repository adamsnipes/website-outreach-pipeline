"""Entry point so the system can be driven as ``python -m outreach``."""

from outreach.cli.main import app


def main() -> None:
    app()


if __name__ == "__main__":  # pragma: no cover - exercised via the CLI
    main()
