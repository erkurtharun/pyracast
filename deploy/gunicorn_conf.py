bind = "0.0.0.0:8000"
workers = 1
worker_class = "gthread"
threads = 2
timeout = 90
graceful_timeout = 90
keepalive = 5
control_socket_disable = True
worker_tmp_dir = "/tmp"  # noqa: S108 -- Private container tmpfs, no shared host directory.
accesslog = "-"
errorlog = "-"
# Query strings and referrers can contain authentication tokens.
access_log_format = "%(m)s %(U)s %(s)s %(L)s"
# Django validates the immediate proxy and overwrites trusted headers itself.
forwarded_allow_ips = ""
secure_scheme_headers: dict[str, str] = {}
