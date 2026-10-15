"""Build the standalone Windows app; run with a Python containing PyInstaller."""
import os
from pathlib import Path
import subprocess
import sys


def main():
    if os.name != "nt":
        print("Build the Windows EXE on Windows.")
        return 1
    root = Path(__file__).resolve().parents[1]
    build = root / "build"
    build.mkdir(exist_ok=True)
    return subprocess.run(
        [
            sys.executable, "-m", "PyInstaller",
            "--noconfirm", "--clean", "--onefile", "--console",
            "--name", "FunPay-Lister",
            "--distpath", str(root / "dist"),
            "--workpath", str(build / "pyinstaller"),
            "--specpath", str(build),
            "--add-data", f"{root / 'static'};static",
            str(root / "app.py"),
        ],
        cwd=root,
    ).returncode


if __name__ == "__main__":
    raise SystemExit(main())
