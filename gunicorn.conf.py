import os

from monitor.logging_config import configured_log_level


bind = os.environ.get("SERVER_MONITOR_BIND", "127.0.0.1:8000")
workers = 1
worker_class = "gevent"
worker_connections = 100
timeout = 360
graceful_timeout = 30
keepalive = 5
preload_app = False
reload = False
accesslog = "-"
errorlog = "-"
capture_output = True
loglevel = configured_log_level()[0].lower()
