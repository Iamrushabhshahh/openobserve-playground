#!/usr/bin/env bash
# Pull the images docker compose needs, but only the missing ones, and retry when a registry
# rate-limits us (public ECR and Docker Hub both do, and CI runners share IP addresses).
set -uo pipefail

missing=()
for image in $(docker compose config --images); do
  docker image inspect "$image" >/dev/null 2>&1 || missing+=("$image")
done
[ ${#missing[@]} -eq 0 ] && exit 0

for attempt in 1 2 3 4; do
  docker compose pull --quiet && exit 0
  [ "$attempt" = 4 ] && break
  wait=$(( attempt * 30 ))
  echo "Pull failed (attempt $attempt of 4). Registries rate-limit busy addresses; trying again in ${wait} s."
  sleep "$wait"
done
echo "Could not pull ${missing[*]}. If the error says 'toomanyrequests', wait a few minutes and run make up again."
exit 1
