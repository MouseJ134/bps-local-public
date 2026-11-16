"""Run synthetic unit and localhost integration tests, emitting safe results."""
from pathlib import Path
import argparse
import hashlib
import json
import sys
import unittest

ROOT = Path(__file__).resolve().parent

def source_hashes():
    return {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted((ROOT / "src").glob("*")) if p.is_file()}

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, default=ROOT / "test-results.json")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    sys.path.insert(0, str(ROOT / "src"))
    sys.path.insert(0, str(ROOT / "tests"))
    before = source_hashes()
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"), pattern="test_*.py")
    result = unittest.TextTestRunner(verbosity=2 if args.verbose else 1).run(suite)
    after = source_hashes()
    report = {
        "scope": "synthetic unit and localhost HTTP/SSE integration; no real upstream or tool execution",
        "tests_run": result.testsRun,
        "success": result.wasSuccessful() and before == after,
        "failures": [test.id() for test, _ in result.failures],
        "errors": [test.id() for test, _ in result.errors],
        "skipped": [test.id() for test, _ in result.skipped],
        "source_unchanged_during_run": before == after,
        "source_sha256": after,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "source_sha256"}, indent=2))
    return 0 if report["success"] else 1

if __name__ == "__main__":
    raise SystemExit(main())
