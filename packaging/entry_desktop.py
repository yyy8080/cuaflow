"""PyInstaller entrypoint for the cuaflow desktop launcher / controller."""

from cuaflow.desktop import main

if __name__ == "__main__":
    raise SystemExit(main())
