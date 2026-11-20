"""Print current lawyer-agreement figures from the configured Postgres store.

The Phase 7 gate records this deliberately small, human-readable snapshot in
``docs/Metrics.md``. Keeping the query behind the store repository means the CLI and API
always use the same decision and dismissal rules.
"""

from __future__ import annotations

from app.store.postgres import postgres_store


def rate(value: float | None) -> str:
    """Format an absent rate distinctly from a real zero."""
    return "—" if value is None else f"{value:.1%}"


def main() -> None:
    store = postgres_store()
    try:
        rows = store.metrics.agreement()
    finally:
        store.close()

    print("| Agent version | Kept | Overridden | Dismissed | Agreement |")
    print("| --- | ---: | ---: | ---: | ---: |")
    for row in rows:
        print(
            f"| {row.agent_version} | {row.kept} | {row.overridden} | "
            f"{row.dismissed} | {rate(row.rate)} |"
        )
    if not rows:
        print("| _No decisions recorded_ | 0 | 0 | 0 | — |")


if __name__ == "__main__":
    main()
