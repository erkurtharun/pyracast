# A runtime secret with SMTP mail and runtime.env as deploy/runtime_secrets.py writes it from that
# secret; the deployment check proof reads the file, the helper's tests hold the two together.
RUNTIME_SECRET = {
    "PYRACAST_SECRET_KEY": "deployment-check-only-6a1f0c9e47d2b835e1c4a7f90d2b6e83c5a1f7d9",
    "PGUSER": "pyracast_app",
    "PGPASSWORD": "runtime-password",
    "RABBITMQ_DEFAULT_PASS": "broker-password",
    "PYRACAST_EMAIL_HOST": "smtp.example.com",
    "PYRACAST_FROM_EMAIL": "Pyracast <info@pyracast.example>",
}
RUNTIME_ENV = {
    "PYRACAST_SECRET_KEY": RUNTIME_SECRET["PYRACAST_SECRET_KEY"],
    "PGUSER": "pyracast_app",
    "PGPASSWORD": "runtime-password",
    "PYRACAST_EMAIL_HOST": "smtp.example.com",
    "PYRACAST_FROM_EMAIL": "Pyracast <info@pyracast.example>",
    "PYRACAST_RABBITMQ_URL": "amqp://pyracast:broker-password@rabbitmq:5672/%2F",
}
