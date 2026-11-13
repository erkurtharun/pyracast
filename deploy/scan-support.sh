#!/usr/bin/env bash
set -euo pipefail
[[ $# == 1 ]] || { echo 'Usage: scan-support.sh GATEWAY_IMAGE' >&2; exit 2; }
root=$(cd "$(dirname "$0")/.." && pwd)
reports=$(mktemp -d)
cache="pyracast-trivy-${GITHUB_RUN_ID:-local}-$$"
container=
cleanup() {
  if [[ -n $container ]]; then docker rm -v "$container" >/dev/null || true; fi
  docker volume rm "$cache" >/dev/null || true
  rm -rf -- "$reports"
}
trap cleanup EXIT
export GATEWAY_IMAGE=$1 BACKEND_IMAGE=unused WEB_IMAGE=unused SITE_ADDRESS=scan.invalid ACME_EMAIL=scan@example.com
export CLOUD_SQL_CONNECTION_NAME=scan:local:database RUNTIME_ENV_FILE=/dev/null BROKER_ENV_FILE=/dev/null MIGRATION_ENV_FILE=/dev/null
docker compose --env-file /dev/null -f "$root/deploy/compose.yaml" config --format json >"$reports/compose.json"
docker volume create "$cache" >/dev/null
for service in gateway rabbitmq sqlproxy; do
  image=$(jq -er --arg service "$service" '.services[$service].image' "$reports/compose.json")
  if [[ $service != gateway && ! $image =~ @sha256:[0-9a-f]{64}$ ]]; then
    echo "Pin the $service image to an immutable registry digest." >&2
    exit 1
  fi
  if ! docker image inspect "$image" >/dev/null 2>&1; then docker pull "$image"; fi
  identity=$(docker image inspect "$image" --format '{{.Id}}')
  printf '%s\n' "$identity" >"$reports/$service.image"
  if [[ $service == gateway ]]; then
    container=$(docker create "$identity")
    docker cp "$container:/usr/bin/caddy" "$reports/caddy"
    docker rm -v "$container" >/dev/null
    container=
    docker run --rm --network none -e SITE_ADDRESS -e ACME_EMAIL -v "$root/deploy:/etc/caddy:ro" \
      "$identity" caddy adapt --config /etc/caddy/Caddyfile --adapter caddyfile >"$reports/adapted.json"
  fi
  docker run --rm -v /var/run/docker.sock:/var/run/docker.sock -v "$reports:/reports" -v "$cache:/cache" \
    aquasec/trivy:0.76.2@sha256:8f6bb947dbc528902e11f2f5be52f7b519f6a39d5d148f41166f2040499768fb image \
    --image-src docker --cache-dir /cache --scanners vuln --severity HIGH,CRITICAL --ignore-unfixed=false \
    --format json --output "/reports/$service.json" --no-progress "$identity"
  jq --arg service "$service" '{service: $service, image: .Metadata.ImageID, findings: [.Results[] | {target: .Target, vulnerabilities: (.Vulnerabilities // [])}]}' \
    "$reports/$service.json"
done
docker run --rm --network none --cap-drop ALL -v "$root/deploy:/source/deploy:ro" -v "$reports:/reports:ro" \
  python:3.15.2-slim-trixie@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928 \
  python /source/deploy/check_support_images.py /reports --root /source
