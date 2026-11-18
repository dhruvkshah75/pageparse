"""Open the Go terminal client without managing model servers by default."""
import os
from pathlib import Path
import sys


def main(manual=True):
    root = Path(sys.prefix).parent
    binary = Path(os.environ.get("PAGEPARSE_TUI", root / "build/pageparse"))
    if not binary.is_file() and not os.environ.get("PAGEPARSE_TUI"):
        binary = root / "run-pageparse"
    if not binary.is_file():
        raise SystemExit("PageParse TUI binary not found. Run ./run-pageparse from the checkout or set PAGEPARSE_TUI.")
    os.environ["PAGEPARSE_PYTHON"] = sys.executable
    args = ["--no-start"] if manual else []
    try:
        os.execv(binary, [str(binary), *args, *sys.argv[1:]])
    except OSError as error:
        raise SystemExit(f"Could not launch the PageParse TUI: {error}") from None


if __name__ == "__main__":
    main()
