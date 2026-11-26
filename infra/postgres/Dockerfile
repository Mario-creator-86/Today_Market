FROM timescale/timescaledb:latest-pg16
USER root
RUN apk add --no-cache --virtual .build-deps git make gcc musl-dev clang llvm-dev \
    && git clone --branch v0.8.0 --depth 1 https://github.com/pgvector/pgvector.git /tmp/pgvector \
    && cd /tmp/pgvector && make && make install \
    && rm -rf /tmp/pgvector && apk del .build-deps
