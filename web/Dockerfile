# syntax=docker/dockerfile:1@sha256:4edf897a3ffa55b89f906fc8cc78afdb3f1834cc9c7083565e611a8a7d5fe99e
FROM node:26.13.1-trixie-slim@sha256:e6bedef217677b276c591795e169840edd9096dbf13f29df8ef8d1856b8820bc AS base
WORKDIR /app
ENV NEXT_TELEMETRY_DISABLED=1

FROM base AS dependencies
COPY package.json package-lock.json ./
RUN --mount=type=cache,target=/root/.npm npm ci --ignore-scripts

FROM base AS build
COPY --from=dependencies /app/node_modules ./node_modules
COPY . .
RUN mkdir -p public && npm run build

FROM base AS runtime-node
RUN apt-get update && apt-get upgrade --no-install-recommends -y \
    && mkdir -p /runtime/usr/local/bin /runtime/usr/lib/x86_64-linux-gnu /runtime/var/lib/dpkg/status.d /runtime/usr/share/doc/libatomic1 \
    && cp /usr/local/bin/node /runtime/usr/local/bin/ \
    && cp /usr/local/LICENSE /runtime/usr/local/ \
    && cp -L /usr/lib/x86_64-linux-gnu/libatomic.so.1 /runtime/usr/lib/x86_64-linux-gnu/ \
    && cp -L /usr/share/doc/libatomic1/copyright /runtime/usr/share/doc/libatomic1/ \
    && dpkg-query --status libatomic1 > /runtime/var/lib/dpkg/status.d/libatomic1 \
    && zstd_version=$(dpkg-query --show --showformat='${Version}' libzstd1) \
    && if dpkg --compare-versions "$zstd_version" lt 1.5.7+dfsg-1+deb13u1; then \
      echo "libzstd1 $zstd_version is older than 1.5.7+dfsg-1+deb13u1" >&2; exit 1; fi \
    && cd /tmp && apt-get download "libzstd1=$zstd_version" \
    && dpkg-deb --extract libzstd1_*_amd64.deb /runtime \
    && dpkg-deb --info libzstd1_*_amd64.deb md5sums > /runtime/var/lib/dpkg/status.d/libzstd1.md5sums \
    && dpkg-query --status libzstd1 > /runtime/var/lib/dpkg/status.d/libzstd1

FROM gcr.io/distroless/cc-debian13:nonroot@sha256:532c7a94f9e96ba9db75eb9388f4227751131c7cb0a411d1a8521af4096135de AS runtime
ENV NODE_ENV=production HOSTNAME=0.0.0.0 PORT=3000
WORKDIR /app
COPY --from=runtime-node /runtime/ /
COPY --from=build --chown=1000:1000 /app/.next/standalone ./
COPY --from=build --chown=1000:1000 /app/.next/static ./.next/static
COPY --from=build --chown=1000:1000 /app/public ./public
USER 1000:1000
EXPOSE 3000
STOPSIGNAL SIGTERM
CMD ["node", "server.js"]
