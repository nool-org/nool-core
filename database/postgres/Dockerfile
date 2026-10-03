# postgres:16-alpine + the pgvector extension (question_embeddings - see
# the question_embeddings migration). Built on the same Alpine base rather
# than switching to pgvector/pgvector (Debian/glibc) so an existing data
# volume keeps its en_US.utf8 collation behaviour - a libc switch would
# silently change text sort order and invalidate existing text indexes.
FROM postgres:16-alpine

ARG PGVECTOR_VERSION=0.8.6

# with_llvm=no: skip JIT bitcode, which would need the exact clang/llvm
# version the base image was built with. OPTFLAGS="" keeps the build
# portable (no -march=native).
RUN apk add --no-cache --virtual .build-deps git build-base \
    && git clone --branch "v${PGVECTOR_VERSION}" --depth 1 https://github.com/pgvector/pgvector.git /tmp/pgvector \
    && make -C /tmp/pgvector with_llvm=no OPTFLAGS="" \
    && make -C /tmp/pgvector with_llvm=no install \
    && rm -rf /tmp/pgvector \
    && apk del .build-deps
