"""Gunicorn settings for the office server.

    gunicorn -c deploy/gunicorn.conf.py app:app

Sized for four logistics staff, not for scale. The point of these numbers is
predictability: enough workers that nobody waits behind someone else's export,
few enough that four SQLite connections are never fighting over the same file.
"""

bind = "127.0.0.1:8090"          # nginx is the only thing that talks to this
workers = 3                       # 2 x CPU + 1 on a small box; 4 staff, so ample
threads = 2                       # a slow export must not block a colleague
worker_class = "gthread"

# A whole-year detailed Excel export is genuinely slow to build — measured at
# ~20s for 120,000 item rows. The default 30s timeout would kill it mid-file and
# hand the operator a broken download.
timeout = 120
graceful_timeout = 30

# nginx holds keep-alive connections open; anything less makes it reconnect.
keepalive = 5

# Restart workers periodically so a slow leak can never accumulate. The jitter
# stops all three restarting at the same moment.
max_requests = 1000
max_requests_jitter = 100

accesslog = "-"                   # journald captures stdout/stderr
errorlog = "-"
loglevel = "info"
# Default access log format plus request duration, so a slow page is visible.
access_log_format = '%(h)s "%(r)s" %(s)s %(b)s %(M)sms "%(f)s"'

proc_name = "gate-pass"
