# syntax=docker/dockerfile:1
FROM python:3.12-slim-bookworm AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /myfish
ENV UV_DEFAULT_INDEX="https://pypi.tuna.tsinghua.edu.cn/simple" \
    UV_LINK_MODE=copy

RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-install-project --no-dev

COPY src/ ./src/
COPY pyproject.toml uv.lock hatch_build.py README.md LICENSE ./

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

FROM python:3.12-slim-bookworm

WORKDIR /myfish
ARG TARGETARCH

COPY --from=builder /bin/uv /bin/uvx /bin/
COPY --from=builder /myfish/ /myfish/
COPY docker-entrypoint.sh /usr/local/bin/myfish-entrypoint

RUN mkdir -p /myfish/plugins /myfish/data

RUN if [ "$TARGETARCH" = "amd64" ]; then \
        rm -f src/myfish/adapters/fish/libs/*arm64* src/myfish/adapters/fish/libs/*.dll; \
    elif [ "$TARGETARCH" = "arm64" ]; then \
        rm -f src/myfish/adapters/fish/libs/sign_core_linux.so src/myfish/adapters/fish/libs/*.dll; \
    fi

ENV PATH="/myfish/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    MYFISH_CONFIG_PATH="/myfish/data/config.toml" \
    UV_DEFAULT_INDEX="https://pypi.tuna.tsinghua.edu.cn/simple" \
    UV_LINK_MODE=copy

VOLUME ["/myfish/plugins", "/myfish/data"]

STOPSIGNAL SIGINT

ENTRYPOINT ["/bin/sh", "/usr/local/bin/myfish-entrypoint"]
CMD ["myfish", "run"]
