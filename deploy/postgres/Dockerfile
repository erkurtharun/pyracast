FROM postgres:18.7-alpine3.24@sha256:75dcac3dfb1cd01ca3536910bbc84128759362f8b118bf8c55b38a94f68f3b73
RUN rm /usr/local/bin/gosu
COPY pg_hba.conf /etc/postgresql/pg_hba.conf
USER 70:70
