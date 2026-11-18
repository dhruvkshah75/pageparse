"""Launch the terminal application."""
from .app import PageParse


def main():
    try:
        app = PageParse()
    except ValueError as error:
        raise SystemExit(str(error)) from None
    app.run()


if __name__ == "__main__":
    main()
