#!/usr/bin/env bash
set -euo pipefail
umask 077
[[ $# == 2 ]] || { echo 'Usage: test-broker-rollback.sh BACKEND_IMAGE RABBITMQ_IMAGE' >&2; exit 2; }
# The public broker of every release before the built one. A failed release returns to the previous
# release on the data the built broker wrote, and RabbitMQ does not guarantee downgrades.
previous=rabbitmq:4.4.0@sha256:1166559c9e77b0208b9d96bfaf265ddbdd491ce3cebb8cf990aabe9274f16d2c
built=$2
export BACKEND_IMAGE=$1 WEB_IMAGE=unused GATEWAY_IMAGE=unused POSTGRES_IMAGE=unused RABBITMQ_IMAGE=$previous
export SITE_ADDRESS=localhost ACME_EMAIL=rollback@example.com
root=$(cd "$(dirname "$0")/.." && pwd)
temporary=$(mktemp -d)
export RUNTIME_ENV_FILE="$temporary/runtime.env" BROKER_ENV_FILE="$temporary/broker.env"
export DATABASE_ENV_FILE=/dev/null MIGRATION_ENV_FILE=/dev/null
compose=(docker compose -p pyracast-broker-rollback -f "$root/deploy/compose.yaml" -f "$root/deploy/compose.smoke.yaml")
cleanup() {
  result=$?
  if (( result != 0 )); then "${compose[@]}" logs --tail 60 rabbitmq; fi
  "${compose[@]}" down --volumes --remove-orphans --timeout 60
  rm -rf -- "$temporary"
  exit "$result"
}
trap cleanup EXIT
cat > "$RUNTIME_ENV_FILE" <<'ENV'
PYRACAST_SECRET_KEY=broker-rollback-only-5d0c8e2b7a41f9366e3b1c04d2a8f75b9e6c1d3a0f4b8e27
PGUSER=pyracast_app
PGPASSWORD=broker-rollback-unused
PYRACAST_RABBITMQ_URL=amqp://pyracast:rollback-broker@rabbitmq/%2F
PYRACAST_EMAIL_HOST=smtp.example.com
PYRACAST_FROM_EMAIL=rollback@example.com
ENV
printf 'RABBITMQ_DEFAULT_USER=pyracast\nRABBITMQ_DEFAULT_PASS=rollback-broker\n' > "$BROKER_ENV_FILE"
start_broker() {
  export RABBITMQ_IMAGE=$1
  "${compose[@]}" up -d --wait --wait-timeout 180 rabbitmq
  [[ $(docker inspect --format '{{.Image}}' "$("${compose[@]}" ps -q rabbitmq)") \
    == "$(docker image inspect --format '{{.Id}}' "$1")" ]]
}
queue_and_count() {
  "${compose[@]}" run --rm -T --no-deps --entrypoint python api - "$1" <<'PY'
import sys
import django
django.setup()
from pyracast.background.broker import make_broker
from pyracast.orchestration.kinds import COLLECTION
broker = make_broker()
try:
    COLLECTION.make_actor(broker).send(0)
    ready, _, _ = broker.get_queue_message_counts('default')
finally:
    broker.close()
assert ready == int(sys.argv[1]), f'{ready} messages queued, expected {sys.argv[1]}'
PY
}
"${compose[@]}" run --rm --no-deps --user 0 --cap-add CHOWN --entrypoint sh rabbitmq -c 'chown -R 999:999 /var/lib/rabbitmq'
start_broker "$previous"
queue_and_count 1
start_broker "$built"
queue_and_count 2
start_broker "$previous"
queue_and_count 3
echo 'The built broker kept the work the previous public broker queued, that broker kept the work the built one queued, and each accepted more.'
