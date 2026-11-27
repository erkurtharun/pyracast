FROM postgres:18.7-alpine3.24@sha256:0115267ab5a509022df9319c77be365cef335adb71556c75f60f5360ee247e71
RUN rm /usr/local/bin/gosu
# Drop once the pinned base image carries libxml2 2.13.9-r3 or later.
RUN apk add --no-cache 'libxml2>=2.13.9-r3'
COPY pg_hba.conf /etc/postgresql/pg_hba.conf
USER 70:70
