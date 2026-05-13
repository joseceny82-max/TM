"""runners/test_runner.py — Punto de entrada para tests QA.

Por ahora simplemente delega a pytest sobre `tests/`.

Uso:
    python -m runners.test_runner
    python -m runners.test_runner -k test_intl
    python -m runners.test_runner --verbose
"""
import os
import subprocess
import sys


def main(args: list = None) -> int:
    if args is None:
        args = sys.argv[1:]
    tests_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "tests")
    cmd = [sys.executable, "-m", "pytest", tests_dir] + list(args)
    return subprocess.call(cmd)


if __name__ == "__main__":
    sys.exit(main())
