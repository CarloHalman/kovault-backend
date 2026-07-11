# kovault-db image — PostgreSQL 16 + pgvector + pg_search.
#
# Default path (BUILD.md B5 resolved to recursive-CTE graph, so no Apache AGE): base on
# ParadeDB, which ships pg_search AND pgvector prebuilt — zero custom compile. This is the
# "stock paradedb/paradedb, zero custom build" outcome noted in QUESTIONS.md A2.
#
# PIN THIS before production. Pick a tag built on Postgres 16 from https://hub.docker.com/r/paradedb/paradedb/tags
FROM paradedb/paradedb:0.24.1-pg16

# First boot runs these in name order (standard postgres entrypoint):
#   01-schema.sql (extensions + full schema) then 02-init.sql (settings seed).
COPY 01-schema.sql 02-init.sql /docker-entrypoint-initdb.d/

# ---------------------------------------------------------------------------------------
# ALTERNATIVE — compile all extensions from source on postgres:16, incl. Apache AGE.
# Use this instead of the FROM above only if you re-enable AGE (uncomment the age lines in
# 01-schema.sql / 02-init.sql and add ',age' to shared_preload_libraries in compose).
# ---------------------------------------------------------------------------------------
# FROM postgres:16 AS build
# RUN apt-get update && apt-get install -y --no-install-recommends \
#       build-essential git ca-certificates curl pkg-config libclang-dev \
#       postgresql-server-dev-16 && rm -rf /var/lib/apt/lists/*
# RUN git clone --branch v0.8.0 --depth 1 https://github.com/pgvector/pgvector.git /tmp/pgvector && \
#     make -C /tmp/pgvector && make -C /tmp/pgvector install
# RUN curl -sSf https://sh.rustup.rs | sh -s -- -y
# ENV PATH="/root/.cargo/bin:${PATH}"
# RUN git clone --branch <pin-tag> --depth 1 https://github.com/paradedb/paradedb.git /tmp/paradedb && \
#     cargo install cargo-pgrx --version <match-paradedb> --locked && \
#     cargo pgrx init --pg16 $(which pg_config) && \
#     cd /tmp/paradedb/pg_search && cargo pgrx install --release
# RUN git clone --branch release/PG16/1.5.0 --depth 1 https://github.com/apache/age.git /tmp/age && \
#     make -C /tmp/age && make -C /tmp/age install
# FROM postgres:16
# COPY --from=build /usr/lib/postgresql/16/lib/         /usr/lib/postgresql/16/lib/
# COPY --from=build /usr/share/postgresql/16/extension/ /usr/share/postgresql/16/extension/
# COPY 01-schema.sql 02-init.sql /docker-entrypoint-initdb.d/
