"""What never goes into a bundle: fixed exclusions, hygiene exclusions, then ``.sscignore``.

``.git`` is left out as a file too (a worktree or submodule checkout). ``.sscignore`` uses
gitignore syntax, negation included, but cannot re-include a fixed or hygiene exclusion.
``dist/`` and ``build/`` are kept: static sites ship them.
"""

from collections.abc import Iterable
from pathlib import Path
from typing import Final

from pathspec import GitIgnoreSpec

FIXED_PATTERNS: Final = (".env", ".env.*", "node_modules/", ".git")
HYGIENE_PATTERNS: Final = (".venv/", "__pycache__/", ".DS_Store")
IGNORE_FILE: Final = ".sscignore"
ENV_REASON: Final = ".env"


def is_env_name(name: str) -> bool:
    """``.env`` or ``.env.*`` in any letter case: never uploaded, refused by the server."""
    folded = name.casefold()
    return folded == ".env" or folded.startswith(".env.")


class Ignore:
    def __init__(self, sscignore: Iterable[str] = ()) -> None:
        self._fixed = [
            (p, GitIgnoreSpec.from_lines([p]))
            for p in (*FIXED_PATTERNS, *HYGIENE_PATTERNS)
            if not p.startswith(".env")
        ]
        self._user = GitIgnoreSpec.from_lines(list(sscignore))

    @classmethod
    def load(cls, root: Path) -> Ignore:
        path = root / IGNORE_FILE
        if not path.is_file():
            return cls()
        return cls(path.read_text(encoding="utf-8", errors="replace").splitlines())

    def reason(self, rel: str, *, is_dir: bool) -> str | None:
        """Why ``rel`` (POSIX, relative to the root) is left out, or None to keep it."""
        if is_env_name(rel.rsplit("/", 1)[-1]):
            return ENV_REASON
        path = f"{rel}/" if is_dir else rel
        for pattern, spec in self._fixed:
            if spec.match_file(path):
                return pattern
        if self._user.match_file(path):
            return IGNORE_FILE
        return None
