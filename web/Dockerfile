# syntax=docker/dockerfile:1@sha256:4edf897a3ffa55b89f906fc8cc78afdb3f1834cc9c7083565e611a8a7d5fe99e
FROM node:26.13.0-trixie-slim@sha256:5adad64f9a1fca2d51972caa65983634c8a774eb300d72b20f7f2a0f60368072 AS base
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
    && dpkg-query --status libatomic1 > /runtime/var/lib/dpkg/status.d/libatomic1

FROM gcr.io/distroless/cc-debian13:nonroot@sha256:cb87acc574abe719b6e5c547384defa2f7b1f24af9605d3f618cf852495d8be2 AS runtime
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
