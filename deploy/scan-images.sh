#!/usr/bin/env bash
set -euo pipefail
[[ $# == 5 ]] || { echo 'Usage: scan-images.sh BACKEND_IMAGE WEB_IMAGE GATEWAY_IMAGE POSTGRES_IMAGE RABBITMQ_IMAGE' >&2; exit 2; }
root=$(cd "$(dirname "$0")/.." && pwd)
reports=$(mktemp -d)
cache="pyracast-trivy-${GITHUB_RUN_ID:-local}-$$"
cleanup() {
  docker volume rm "$cache" >/dev/null || true
  rm -rf -- "$reports"
}
trap cleanup EXIT
export BACKEND_IMAGE=$1 WEB_IMAGE=$2 GATEWAY_IMAGE=$3 POSTGRES_IMAGE=$4 RABBITMQ_IMAGE=$5 SITE_ADDRESS=scan.invalid ACME_EMAIL=scan@example.com
export DATABASE_ENV_FILE=/dev/null RUNTIME_ENV_FILE=/dev/null BROKER_ENV_FILE=/dev/null MIGRATION_ENV_FILE=/dev/null
docker compose --env-file /dev/null -f "$root/deploy/compose.yaml" config --format json >"$reports/compose.json"
docker volume create "$cache" >/dev/null
for service in backend web gateway rabbitmq postgres; do
  case $service in
    backend) image=$BACKEND_IMAGE ;;
    web) image=$WEB_IMAGE ;;
    *) image=$(jq -er --arg service "$service" '.services[$service].image' "$reports/compose.json") ;;
  esac
  identity=$(docker image inspect "$image" --format '{{.Id}}')
  printf '%s\n' "$identity" >"$reports/$service.image"
  docker run --rm -v /var/run/docker.sock:/var/run/docker.sock -v "$reports:/reports" -v "$cache:/cache" \
    aquasec/trivy:0.76.2@sha256:8f6bb947dbc528902e11f2f5be52f7b519f6a39d5d148f41166f2040499768fb image \
    --image-src docker --cache-dir /cache --scanners vuln --severity HIGH,CRITICAL --ignore-unfixed=false \
    --ignorefile /dev/null --list-all-pkgs \
    --format json --output "/reports/$service.json" --no-progress "$identity"
done
docker run --rm --network none --cap-drop ALL --user "$(id -u):$(id -g)" \
  -v "$root/deploy:/source/deploy:ro" -v "$reports:/reports:ro" \
  python:3.15.2-slim-trixie@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928 \
  python /source/deploy/check_image_scans.py /reports
