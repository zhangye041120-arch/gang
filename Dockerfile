ARG PYTHON_IMAGE=python:3.12-slim-bookworm@sha256:a116514e19457bcb7af7efe9c3dd0b9b71e85b317694e7882a1c52aa15a78134

FROM ${PYTHON_IMAGE} AS builder
WORKDIR /build
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1
COPY requirements.txt .
RUN python -m pip wheel --require-hashes --wheel-dir /wheels -r requirements.txt

FROM ${PYTHON_IMAGE} AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1
RUN groupadd --gid 10001 xiaoliao \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin xiaoliao
WORKDIR /app
COPY --from=builder /wheels /wheels
COPY requirements.txt .
RUN python -m pip install --no-index --find-links=/wheels --require-hashes -r requirements.txt \
    && rm -rf /wheels
COPY . .
RUN chown -R 10001:10001 /app
USER 10001:10001
EXPOSE 8081
CMD ["python", "run_api.py"]
