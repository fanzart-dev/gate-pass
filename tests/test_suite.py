"""pytest entry point.

    pytest tests/ --verbose

The real suite is `tests/test_flow.py`, a plain script that reports 871 named
checks. It is not written as pytest functions and is not being rewritten as
them: the checks are the specification of this app, several were added the hour
a bug was found, and rewriting eight hundred assertions to satisfy a runner is
a good way to lose one quietly.

So this module runs that suite and surfaces each of its checks as a pytest
test. `pytest tests/ --verbose` lists them individually and fails on exactly
the ones that failed; `python3 tests/test_flow.py` still works unchanged and
stays the faster way to run it.

The parser, API, permission and print-geometry coverage all lives there —
see its docstring for what it covers.
"""

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
SUITE = Path(__file__).parent / "test_flow.py"


def _run_suite():
    """Run the suite once and return (checks, skipped, output).

    Once per session, not once per check: the suite builds databases, parses
    PDFs and renders pages, so running it per assertion would take minutes
    instead of seconds.
    """
    proc = subprocess.run(
        [sys.executable, str(SUITE)], cwd=str(ROOT),
        capture_output=True, text=True,
    )
    output = proc.stdout + proc.stderr
    checks, skipped = [], []
    for line in output.splitlines():
        if line.startswith("FAIL: "):
            checks.append((line[6:].strip(), False))
        elif line.startswith("  - "):
            skipped.append(line[4:].strip())
    return checks, skipped, output, proc.returncode


@pytest.fixture(scope="session")
def suite_result():
    return _run_suite()


def test_suite_passes(suite_result):
    """Every check in tests/test_flow.py."""
    failures, skipped, output, code = suite_result
    if failures:
        listed = "\n".join(f"  FAIL: {name}" for name, _ in failures)
        pytest.fail(f"{len(failures)} check(s) failed:\n{listed}", pytrace=False)
    assert code == 0, f"the suite exited {code}:\n{output[-3000:]}"


def test_reports_how_many_checks_ran(suite_result):
    """Guards against the suite silently running nothing at all."""
    _failures, _skipped, output, _code = suite_result
    line = next((l for l in output.splitlines() if "checks passed" in l), "")
    assert line, f"no summary line in:\n{output[-2000:]}"
    passed = int(line.split("/")[0].strip())
    assert passed > 500, f"only {passed} checks ran — is the suite being cut short?"


def test_sample_invoices_are_reported_when_absent(suite_result):
    """A clone without the private sample invoices must SAY so, not go quiet."""
    _failures, skipped, output, _code = suite_result
    if skipped:
        assert "sample invoices are not in the repository" in output
