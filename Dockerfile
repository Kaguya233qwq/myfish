# 环境准备
FROM python:3.12-slim-bookworm AS builder
RUN sed -i 's/deb.debian.org/mirrors.tuna.tsinghua.edu.cn/g' /etc/apt/sources.list.d/debian.sources

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /myfish
ENV UV_DEFAULT_INDEX="https://pypi.tuna.tsinghua.edu.cn/simple"
ENV UV_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple

RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-install-project --no-dev

# 运行

FROM python:3.12-slim-bookworm
WORKDIR /myfish

ARG TARGETARCH

COPY --from=builder /myfish/.venv /myfish/.venv
COPY src/ ./src/
COPY pyproject.toml uv.lock ./

RUN mkdir -p /myfish/plugins /myfish/data

RUN if [ "$TARGETARCH" = "amd64" ]; then \
        rm -f src/myfish/adapters/fish/libs/*arm64* src/myfish/adapters/fish/libs/*.dll; \
    elif [ "$TARGETARCH" = "arm64" ]; then \
        rm -f src/myfish/adapters/fish/libs/sign_core_linux.so src/myfish/adapters/fish/libs/*.dll; \
    fi

VOLUME ["/myfish/plugins", "/myfish/data"]

ENV PATH="/myfish/.venv/bin:$PATH"
ENV PYTHONPATH="/myfish/src:$PYTHONPATH"

CMD ["python", "src/myfish/main.py"]