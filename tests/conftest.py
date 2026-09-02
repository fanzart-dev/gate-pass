"""pytest configuration.

`test_flow.py` is deliberately NOT collected by pytest, and this is the whole
reason this file exists.

Its functions are named `test_*` and take a `tmpdir`, so pytest happily picks
them up and runs them — but they report through `check(name, condition)`, which
records a result and carries on rather than raising. A function whose every
check failed still returns normally, so pytest marks it **passed**. Sixty-four
green ticks, hundreds of broken assertions, and nothing to see.

That is the worst failure a test suite can have, so the file is excluded here
and reached only through `test_suite.py`, which runs it as a subprocess and
fails on its actual exit code and its actual FAIL lines.

    pytest tests/ --verbose      the suite, via test_suite.py
    python3 tests/test_flow.py   the same suite, directly and faster
"""

collect_ignore = ["test_flow.py", "make_invoice_pdf.py", "preview_server.py",
                   "benchmark.py", "verify_real_invoice.py", "loadtest.py"]
