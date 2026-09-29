from pathlib import Path

CACHE = Path.home() / ".cache" / "report"


def main() -> None:
    CACHE.mkdir(parents=True, exist_ok=True)
    (CACHE / "last.txt").write_text("done")


if __name__ == "__main__":
    main()
