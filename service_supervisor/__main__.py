"""Run the local operations Supervisor without starting Django first."""

from __future__ import annotations

from pathlib import Path

from .manager import run_supervisor


def main() -> None:
    project_root = Path(__file__).resolve().parent.parent
    raise SystemExit(run_supervisor(project_root, project_root / "runtime"))


if __name__ == "__main__":
    main()
