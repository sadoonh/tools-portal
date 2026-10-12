# Minimal official-agent kernel; no AWS SDK, data credentials or optional surfaces.
FROM ghcr.io/astral-sh/uv:0.12.1@sha256:cf4eedcaa81655197f625739489effcbe71b61ceb1506f332c3facae5deceded AS uv
FROM python:3.12.15-alpine3.23@sha256:9ce5a4e59b833c002e1c4196c60ec0b38977e3235d626298ba7ffe2e64f5a30c
COPY --from=uv /uv /uvx /usr/local/bin/
ENV LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    HOME=/home/notebook \
    UV_PROJECT_ENVIRONMENT=/opt/marimohub/venv \
    UV_CACHE_DIR=/opt/marimohub/uv-cache \
    UV_PYTHON_DOWNLOADS=never \
    VIRTUAL_ENV=/opt/marimohub/venv \
    PATH=/opt/marimohub/venv/bin:$PATH \
    MARIMO_SKIP_UPDATE_CHECK=1 \
    _MARIMO_APP_OVERLOAD_AUTO_DOWNLOAD=[html]
# Alpine/musl avoids the unpatched critical Debian curl/GnuTLS dependencies.
# Record current security patches/digest/scan, not a byte-reproducibility claim.
RUN apk upgrade --no-cache && apk add --no-cache git ca-certificates \
    && addgroup -g 10001 notebook \
    && adduser -D -u 10001 -G notebook notebook \
    && mkdir -p /opt/marimohub /workspace \
    && chmod 1777 /tmp
COPY requirements.txt /tmp/kernel-requirements.txt
RUN uv venv --python /usr/local/bin/python3 /opt/marimohub/venv \
    && uv pip sync --python /opt/marimohub/venv/bin/python /tmp/kernel-requirements.txt \
    && rm /tmp/kernel-requirements.txt \
    && printf '%s\n' 'export PATH="$VIRTUAL_ENV/bin:$PATH"' > /etc/profile.d/venv-path.sh
COPY fargate_agent.py LICENSE /opt/marimohub/
RUN echo '39885ebf326f2255245666a1e428416c54bbfb9809132740442ae73122b9f52d  /opt/marimohub/fargate_agent.py' | sha256sum -c - \
    && chmod 755 /opt/marimohub/fargate_agent.py \
    && chown -R 10001:10001 /opt/marimohub /workspace
USER 10001:10001
WORKDIR /workspace
EXPOSE 2717 2718
CMD ["/usr/local/bin/python3", "/opt/marimohub/fargate_agent.py"]
