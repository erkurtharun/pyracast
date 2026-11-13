# syntax=docker/dockerfile:1@sha256:4edf897a3ffa55b89f906fc8cc78afdb3f1834cc9c7083565e611a8a7d5fe99e
FROM ghcr.io/astral-sh/uv:0.13.8@sha256:5cc1805e2426484abca4ad46ea783bab281fdb225fd82d8ce6d66a81254bac85 AS uv
FROM python:3.15.2-slim-trixie@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928 AS base
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH" \
    DJANGO_SETTINGS_MODULE=pyracast.production \
    PYRACAST_DEBUG=0 PYRACAST_STATIC_ROOT=/app/staticfiles
WORKDIR /app

FROM base AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1
RUN apt-get update && apt-get install --no-install-recommends -y build-essential \
    && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project --no-editable
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev --no-editable
RUN PYRACAST_SECRET_KEY=build-only-not-a-runtime-secret \
    PYRACAST_PUBLIC_ORIGIN=https://build.invalid pyracast collectstatic --noinput

FROM base AS runtime-python
RUN apt-get update && apt-get upgrade --no-install-recommends -y \
    && mkdir -p /runtime/usr/local/bin /runtime/usr/local/lib /runtime/usr/lib/x86_64-linux-gnu /runtime/var/lib/dpkg/status.d \
    && cp -a /usr/local/bin/python3.15 /runtime/usr/local/bin/ \
    && ln -s python3.15 /runtime/usr/local/bin/python3 \
    && ln -s python3.15 /runtime/usr/local/bin/python \
    && cp -a /usr/local/lib/libpython3.15.so.1.0 /usr/local/lib/python3.15 /runtime/usr/local/lib/ \
    && rm -rf /runtime/usr/local/lib/python3.15/site-packages /runtime/usr/local/lib/python3.15/ensurepip \
      /runtime/usr/local/lib/python3.15/idlelib /runtime/usr/local/lib/python3.15/sqlite3 \
      /runtime/usr/local/lib/python3.15/tkinter /runtime/usr/local/lib/python3.15/curses \
      /runtime/usr/local/lib/python3.15/dbm \
    && rm -f /runtime/usr/local/lib/python3.15/lib-dynload/_sqlite3*.so \
      /runtime/usr/local/lib/python3.15/lib-dynload/readline*.so \
      /runtime/usr/local/lib/python3.15/lib-dynload/_curses*.so \
      /runtime/usr/local/lib/python3.15/lib-dynload/_tkinter*.so \
      /runtime/usr/local/lib/python3.15/lib-dynload/_dbm*.so \
      /runtime/usr/local/lib/python3.15/lib-dynload/_gdbm*.so \
      /runtime/usr/local/lib/python3.15/lib-dynload/_uuid*.so \
    && cp -L /usr/lib/x86_64-linux-gnu/libbz2.so.1.0 /usr/lib/x86_64-linux-gnu/liblzma.so.5 \
      /usr/lib/x86_64-linux-gnu/libffi.so.8 /usr/lib/x86_64-linux-gnu/libmpdec.so.4 /runtime/usr/lib/x86_64-linux-gnu/ \
    && for package in libbz2-1.0 liblzma5 libffi8 libmpdec4; do \
      dpkg-query --status "$package" > "/runtime/var/lib/dpkg/status.d/$package"; \
      mkdir -p "/runtime/usr/share/doc/$package"; \
      cp -L "/usr/share/doc/$package/copyright" "/runtime/usr/share/doc/$package/"; \
      done

# This PostgreSQL server runtime excludes package managers and interactive/database tools.
FROM gcr.io/distroless/cc-debian13:nonroot@sha256:cb87acc574abe719b6e5c547384defa2f7b1f24af9605d3f618cf852495d8be2 AS runtime
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:/usr/local/bin:$PATH" LD_LIBRARY_PATH=/usr/local/lib \
    DJANGO_SETTINGS_MODULE=pyracast.production \
    PYRACAST_DEBUG=0 PYRACAST_STATIC_ROOT=/app/staticfiles
WORKDIR /app
COPY --from=runtime-python /runtime/ /
COPY --from=build /app/.venv /app/.venv
COPY --from=build /app/staticfiles /app/staticfiles
COPY deploy/gunicorn_conf.py /app/gunicorn.conf.py
USER 65532:65532
EXPOSE 8000
STOPSIGNAL SIGTERM
CMD ["gunicorn", "--config", "/app/gunicorn.conf.py", "pyracast.wsgi:application"]
