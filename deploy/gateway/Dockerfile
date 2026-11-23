# syntax=docker/dockerfile:1@sha256:4edf897a3ffa55b89f906fc8cc78afdb3f1834cc9c7083565e611a8a7d5fe99e
FROM golang:1.27.4-trixie@sha256:f18c3d73a184fb0582828510722e723b63a93f7188d11dd223d9be6f6880e65d AS source
WORKDIR /src
COPY go.mod go.sum ./
COPY pkg/config/dynamic/ext ./pkg/config/dynamic/ext

FROM source AS lock
RUN --mount=type=cache,target=/go/pkg/mod go mod tidy && go mod download all && go mod verify

FROM scratch AS lockfiles
COPY --from=lock /src/go.mod /src/go.sum /

FROM source AS build
RUN --mount=type=cache,target=/go/pkg/mod go mod download && go mod verify
RUN --mount=type=cache,target=/go/pkg/mod --mount=type=cache,target=/root/.cache/go-build \
    version="$(go list -mod=readonly -m -f '{{.Version}}' github.com/traefik/traefik/v3)" \
    && CGO_ENABLED=0 go build -p 2 -mod=readonly -buildvcs=false -trimpath \
    -ldflags="-s -w -X github.com/traefik/traefik/v3/pkg/version.Version=${version#v}" \
    -o /out/traefik github.com/traefik/traefik/v3/cmd/traefik \
    && cp "$(go env GOMODCACHE)/github.com/traefik/traefik/v3@$version/LICENSE.md" /out/LICENSE.md

FROM gcr.io/distroless/static-debian13:nonroot@sha256:aafe993665bf6200db38a02c11a9351e782a84b6a45e7f33b6695638c88e3931 AS runtime
WORKDIR /
COPY --from=build --chown=0:0 --chmod=0555 /out/traefik /traefik
COPY --from=build /out/LICENSE.md /usr/share/licenses/traefik/LICENSE.md
USER 10001:10001
EXPOSE 8080 8443
ENTRYPOINT ["/traefik"]
