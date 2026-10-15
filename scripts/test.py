"""Portable offline regression runner; run from any working directory."""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest.mock import patch

root = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(root), str(root / "tests")]
import app

parser = argparse.ArgumentParser()
parser.add_argument("--python-only", action="store_true")
args = parser.parse_args()
os.chdir(root)
suite = unittest.defaultTestLoader.discover(str(root / "tests"), pattern="test*.py")
with patch.object(app, "CLEANUP_DELAY", 0):
    result = unittest.TextTestRunner(verbosity=1).run(suite)
passed = result.wasSuccessful()
if not args.python_only:
    node = shutil.which("node")
    if not node:
        print("Node.js is required for JavaScript checks. Install Node.js or use --python-only.")
        passed = False
    else:
        for test in sorted((root / "tests").glob("test*.cjs")):
            passed = subprocess.run([node, str(test)], cwd=root).returncode == 0 and passed
raise SystemExit(0 if passed else 1)
