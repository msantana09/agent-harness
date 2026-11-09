"""`python3 -m codex_jobs`: the console entry, which resolves the real paths for `cli.main`.

Jobs live under `$XDG_DATA_HOME/agent-harness/codex-jobs` (`~/.local/share` when the variable is
unset). The model catalog and Codex's session files are read from `~/.codex`.
"""

import os
import sys
from pathlib import Path

from .cli import main


def job_root() -> Path:
    """The job root in the user's data folder: `$XDG_DATA_HOME`, which defaults to
    `~/.local/share`."""
    data = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(data) / "agent-harness" / "codex-jobs"


def run() -> int:
    home = Path.home()
    return main(
        sys.argv[1:],
        root=job_root(),
        catalog_path=home / ".codex/models_cache.json",
        models_sh_path=Path(__file__).resolve().parents[1] / "models.sh",
        sessions_dir=home / ".codex/sessions",
    )


if __name__ == "__main__":
    sys.exit(run())
