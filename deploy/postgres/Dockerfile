FROM postgres:18.7-alpine3.24@sha256:75dcac3dfb1cd01ca3536910bbc84128759362f8b118bf8c55b38a94f68f3b73
RUN rm /usr/local/bin/gosu
# Drop once the pinned base image carries libxml2 2.13.9-r3 or later.
RUN apk add --no-cache 'libxml2>=2.13.9-r3'
COPY pg_hba.conf /etc/postgresql/pg_hba.conf
USER 70:70
