#!/usr/bin/env bash
set -euo pipefail
[[ $# == 3 || $# == 4 ]] || { echo 'Usage: oci-images.sh load|publish DIRECTORY SHA [REGISTRY]' >&2; exit 2; }
mode=$1
directory=$(cd "$2" && pwd)
revision=$3
[[ "$revision" =~ ^[0-9a-f]{40}$ ]]
# Upstream immutable tags retain release digests across daily rebuilds.
skopeo=quay.io/containers/skopeo:v1.24.1-immutable@sha256:624827441117604d93ee87fa47fe003d1649f20c12eeaa5672fd19a86df5ead3
options=(--rm --cap-drop ALL --security-opt no-new-privileges -v "$directory:/images:ro")

case "$mode" in
  load)
    for image in backend web gateway postgres; do
      docker run "${options[@]}" -v /var/run/docker.sock:/var/run/docker.sock "$skopeo" \
        --override-os linux --override-arch amd64 copy \
        "oci-archive:/images/$image.oci.tar" "docker-daemon:pyracast-$image:ci"
      expected=$(jq -er --arg image "$image" '.images[$image].config_digest' "$directory/images.json")
      actual=$(docker run "${options[@]}" -v /var/run/docker.sock:/var/run/docker.sock "$skopeo" \
        inspect --raw "docker-daemon:pyracast-$image:ci" | jq -er '.config.digest')
      test "$actual" = "$expected"
      test "$(docker image inspect "pyracast-$image:ci" --format '{{index .Config.Labels "org.opencontainers.image.revision"}}')" = "$revision"
    done
    ;;
  publish)
    registry=${4:?Registry is required}
    [[ "$registry" =~ ^[a-z0-9-]+-docker\.pkg\.dev/[a-z0-9-]+/[a-z0-9_-]+$ ]]
    : "${REGISTRY_ACCESS_TOKEN:?Registry access token is required}"
    umask 077
    credentials=$(mktemp -d)
    trap 'rm -rf -- "$credentials"' EXIT
    options+=(--user "$(id -u):$(id -g)" -v "$credentials:/credentials")
    printf '%s' "$REGISTRY_ACCESS_TOKEN" | docker run -i "${options[@]}" "$skopeo" login \
      --authfile /credentials/auth.json --username oauth2accesstoken --password-stdin "${registry%%/*}"
    unset REGISTRY_ACCESS_TOKEN
    for image in backend web gateway postgres; do
      expected=$(jq -er --arg image "$image" '.images[$image].digest' "$directory/images.json")
      [[ "$expected" =~ ^sha256:[0-9a-f]{64}$ ]]
      docker run "${options[@]}" "$skopeo" copy --all --preserve-digests \
        --authfile /credentials/auth.json --digestfile "/credentials/$image.digest" \
        "oci-archive:/images/$image.oci.tar" "docker://$registry/$image:$revision"
      test "$(cat "$credentials/$image.digest")" = "$expected"
      actual=$(docker run "${options[@]}" "$skopeo" inspect --authfile /credentials/auth.json \
        --raw "docker://$registry/$image@$expected" | sha256sum | cut -d ' ' -f 1)
      test "sha256:$actual" = "$expected"
      printf '%s_IMAGE=%s/%s@%s\n' "${image^^}" "$registry" "$image" "$expected" >> "${GITHUB_ENV:?}"
    done
    ;;
  *) echo 'Expected load or publish.' >&2; exit 2 ;;
esac
