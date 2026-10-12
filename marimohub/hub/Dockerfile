# Preserve the exact official application, replacing only the vulnerable OS base.
FROM ghcr.io/marimo-team/marimohub@sha256:e8988391cfd83431da8545a833652edf9496184a673ef865f99749843009ab7f AS upstream
FROM node:24.18.0-alpine3.23@sha256:00295958df3ca22b4d3df4ae8dbffd823dbca1c53efbcf235f62fbf3fa5ca756
RUN apk upgrade --no-cache \
    && addgroup -g 10001 hub \
    && adduser -D -u 10001 -G hub hub
COPY --from=upstream /app /app
WORKDIR /app
ENV NODE_ENV=production \
    PORT=3000 \
    MARIMOHUB_STATIC_ROOT=./public \
    MARIMOHUB_VERSION=0.4.16 \
    MARIMOHUB_IMAGE=marimohub-0.4.16-alpine-security-rebase
USER 10001:10001
EXPOSE 3000
CMD ["node", "dist/index.mjs"]
