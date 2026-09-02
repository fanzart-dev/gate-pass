#!/usr/bin/env python3
"""Hammer a running instance the way the office would, and report what breaks.

    python3 tests/loadtest.py                          # 5 users against :8090
    python3 tests/loadtest.py --url http://fanzart-server.local --users 10
    python3 tests/loadtest.py --user dinesh --password ... --issue

Standard library only — no ab, no locust, nothing to install on the server. It
signs in as real accounts and drives the real pages, which is the point: `ab -n
100 -c 5 http://127.0.0.1:8090/` measures how fast the app can serve a redirect
to the login page, because every one of those requests is unauthenticated.

WITHOUT --issue it is read-only and safe against the live book: sign in, open
the register, open a printed pass. WITH --issue it uploads invoices and issues
real gate passes, which spend real numbers — so it refuses to do that unless
you also pass --i-know-this-writes-to-the-book.

What it is actually looking for:

  * the slow tail, not the average — one 9-second page ruins a morning while a
    good mean hides it;
  * 'database is locked', which is the failure this app was designed to make
    impossible and the one worth proving stays impossible;
  * whether concurrency helps at all, i.e. whether the workers are really
    working in parallel.
"""

import argparse
import http.cookiejar
import statistics
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter


class Client:
    """One browser: its own cookie jar, so sessions do not bleed together."""

    def __init__(self, base):
        self.base = base.rstrip("/")
        self.jar = http.cookiejar.CookieJar()
        # Certificates on this network are signed by a local CA that this
        # script has no reason to know about, and it is only ever pointed at
        # the office's own server, so verification is off deliberately.
        import ssl
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar),
            urllib.request.HTTPSHandler(context=ctx),
        )

    def get(self, path):
        started = time.perf_counter()
        try:
            with self.opener.open(self.base + path, timeout=60) as r:
                r.read()
                return time.perf_counter() - started, r.status
        except urllib.error.HTTPError as exc:
            return time.perf_counter() - started, exc.code
        except Exception as exc:  # noqa: BLE001 - reported, not raised
            return time.perf_counter() - started, type(exc).__name__

    def post(self, path, fields):
        data = urllib.parse.urlencode(fields).encode()
        req = urllib.request.Request(self.base + path, data=data,
                                      headers={"Origin": self.base})
        started = time.perf_counter()
        try:
            with self.opener.open(req, timeout=60) as r:
                r.read()
                return time.perf_counter() - started, r.status, r.geturl()
        except urllib.error.HTTPError as exc:
            return time.perf_counter() - started, exc.code, ""
        except Exception as exc:  # noqa: BLE001
            return time.perf_counter() - started, type(exc).__name__, ""

    def sign_in(self, username, password):
        _t, status, url = self.post("/login", {"username": username,
                                                "password": password})
        return status in (200, 302) and "/login" not in url


def worker(base, username, password, rounds, results, errors, lock):
    client = Client(base)
    if not client.sign_in(username, password):
        with lock:
            errors.append(f"{username}: could not sign in")
        return
    # The pages an operator actually sits on, in the order they visit them.
    journey = ["/register", "/upload", "/register?status=issued", "/"]
    for _ in range(rounds):
        for path in journey:
            elapsed, status = client.get(path)
            with lock:
                results.append((path, elapsed, status))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default="http://127.0.0.1:8090")
    ap.add_argument("--users", type=int, default=5,
                    help="how many people at once (default 5)")
    ap.add_argument("--rounds", type=int, default=10,
                    help="journeys each of them makes (default 10)")
    ap.add_argument("--user", default="demo")
    ap.add_argument("--password", default="demo-preview-only")
    args = ap.parse_args()

    print(f"{args.users} concurrent users x {args.rounds} journeys "
          f"against {args.url}\n")

    results, errors, lock = [], [], threading.Lock()
    threads = [threading.Thread(target=worker,
                                 args=(args.url, args.user, args.password,
                                       args.rounds, results, errors, lock))
               for _ in range(args.users)]

    started = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wall = time.perf_counter() - started

    if errors:
        for e in errors[:5]:
            print(f"  ERROR  {e}")
        print()
    if not results:
        print("Nothing completed — check the URL and the credentials.")
        return 1

    by_path = {}
    for path, elapsed, status in results:
        by_path.setdefault(path, []).append((elapsed, status))

    print(f"{'page':<26}{'n':>5}{'median':>10}{'p95':>10}{'slowest':>10}   codes")
    print("-" * 78)
    for path, rows in sorted(by_path.items()):
        times = sorted(t for t, _ in rows)
        p95 = times[min(len(times) - 1, int(len(times) * 0.95))]
        codes = Counter(str(s) for _, s in rows)
        print(f"{path:<26}{len(times):>5}"
              f"{statistics.median(times) * 1000:>9.0f}ms"
              f"{p95 * 1000:>9.0f}ms{times[-1] * 1000:>9.0f}ms   "
              f"{dict(codes)}")

    times = sorted(t for _p, t, _s in results)
    codes = Counter(str(s) for _p, _t, s in results)
    bad = {c: n for c, n in codes.items() if not c.startswith(("2", "3"))}
    print("-" * 78)
    print(f"{len(results)} requests in {wall:.1f}s "
          f"({len(results) / wall:.0f}/s across {args.users} users)")
    print(f"median {statistics.median(times) * 1000:.0f}ms   "
          f"p95 {times[int(len(times) * 0.95)] * 1000:.0f}ms   "
          f"slowest {times[-1] * 1000:.0f}ms")
    print(f"non-2xx/3xx responses: {bad if bad else 'none'}")

    # The one this app exists to prevent. A locked database under this little
    # load would mean the write path is serialising badly, not that SQLite is
    # too small — see the note on BEGIN IMMEDIATE in db.py.
    locked = sum(1 for _p, _t, s in results if "lock" in str(s).lower())
    print(f"'database is locked': {locked}")

    verdict = not bad and locked == 0 and times[int(len(times) * 0.95)] < 2.0
    print("\n" + ("PASS — comfortable at this concurrency"
                  if verdict else
                  "LOOK AT THIS — slow tail or failed requests above"))
    return 0 if verdict else 1


if __name__ == "__main__":
    sys.exit(main())
