# syntax=docker/dockerfile:1

# Versions of what the image is built from. The upstream versions are
# checked weekly by .github/workflows/upstream.yml, the base images by
# Dependabot.
ARG ALPINE_VERSION=3.24
ARG HDDFANCONTROL_VERSION=2.1.2
ARG HDIDLE_VERSION=v1.21
ARG SDPARM_VERSION=1.12
ARG SDPARM_SHA256=c4c9efafdbeb662e2f9712707ec490932bd4d010bb1129ae7a99526546eeadbe

# --- hddfancontrol, static, so the same file also ships in the bare metal tarball
FROM alpine:${ALPINE_VERSION} AS hddfancontrol
ARG HDDFANCONTROL_VERSION
# A failing command inside a pipe must fail the build.
SHELL ["/bin/ash", "-eo", "pipefail", "-c"]
RUN apk add --no-cache cargo git musl-dev
WORKDIR /src
RUN git clone --depth 1 --branch "${HDDFANCONTROL_VERSION}" https://github.com/desbma/hddfancontrol.git .
# With an explicit --target, RUSTFLAGS reaches only the program: the proc
# macros and build scripts are built for the host and cannot be static.
ENV RUSTFLAGS="-C target-feature=+crt-static" \
    CARGO_PROFILE_RELEASE_STRIP=true
RUN target="$(rustc -vV | sed -n 's/^host: //p')" \
 && cargo build --release --locked --target "${target}" \
 && install -D "target/${target}/release/hddfancontrol" /out/hddfancontrol

# --- hd-idle, cross-compiled: Go needs no emulation for other architectures
FROM --platform=$BUILDPLATFORM golang:1.26-alpine${ALPINE_VERSION} AS hdidle
ARG HDIDLE_VERSION
ARG TARGETOS
ARG TARGETARCH
ARG TARGETVARIANT
RUN apk add --no-cache git
WORKDIR /src
RUN git clone --depth 1 --branch "${HDIDLE_VERSION}" https://github.com/adelolmo/hd-idle.git . \
 && CGO_ENABLED=0 GOOS="${TARGETOS}" GOARCH="${TARGETARCH}" GOARM="${TARGETVARIANT#v}" \
    go build -trimpath -ldflags="-s -w" -o /out/hd-idle .

# --- sdparm: hddfancontrol uses it for the state of SAS drives. Alpine has it
# only in edge/testing, so it is built from the Debian copy of the release.
FROM alpine:${ALPINE_VERSION} AS sdparm
ARG SDPARM_VERSION
ARG SDPARM_SHA256
SHELL ["/bin/ash", "-eo", "pipefail", "-c"]
RUN apk add --no-cache build-base linux-headers
WORKDIR /src
RUN wget -q -O sdparm.tar.xz "https://deb.debian.org/debian/pool/main/s/sdparm/sdparm_${SDPARM_VERSION}.orig.tar.xz" \
 && echo "${SDPARM_SHA256}  sdparm.tar.xz" | sha256sum -c - \
 && tar xJf sdparm.tar.xz --strip-components=1 \
 && ./configure --quiet LDFLAGS=-static \
 && make -s \
 && install -D -s src/sdparm /out/sdparm

# --- the static programs alone, for the bare metal tarball of the release
FROM scratch AS binaries
COPY --from=hddfancontrol /out/hddfancontrol /
COPY --from=hdidle /out/hd-idle /
COPY --from=sdparm /out/sdparm /

# --- the image
FROM alpine:${ALPINE_VERSION}
ARG APP_VERSION=dev
# python3 and aiohttp come built from Alpine for every architecture: no
# compiler, no pip, nothing downloaded at start.
RUN apk add --no-cache \
      hdparm \
      kmod \
      lm-sensors-detect \
      nvme-cli \
      openssl \
      py3-aiohttp \
      python3 \
      smartmontools \
      tini
COPY --from=hddfancontrol /out/hddfancontrol /usr/local/bin/
COPY --from=hdidle /out/hd-idle /usr/local/bin/
COPY --from=sdparm /out/sdparm /usr/local/bin/
# The gateway (the part the network reaches) runs as this user, not root.
RUN addgroup -S -g 10001 hddfancontrol-webui \
 && adduser -S -D -H -u 10001 -G hddfancontrol-webui -h /nonexistent -s /sbin/nologin hddfancontrol-webui
COPY hddfancontrol_webui /app/hddfancontrol_webui
# The root file system can be read only: compile the bytecode now.
RUN python3 -m compileall -q /app/hddfancontrol_webui
LABEL org.opencontainers.image.title="hddfancontrol_webui" \
      org.opencontainers.image.description="Web UI for hddfancontrol and hd-idle" \
      org.opencontainers.image.source="https://github.com/anto294/hddfancontrol_webui" \
      org.opencontainers.image.licenses="GPL-3.0-or-later" \
      org.opencontainers.image.version="${APP_VERSION}"
ENV APP_VERSION=${APP_VERSION} \
    CONFIG_DIR=/config \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app
VOLUME /config
EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=5s --start-period=60s \
  CMD ["/bin/sh", "-c", "wget -q -O /dev/null \"http://127.0.0.1:${WEBUI_PORT:-8080}/healthz\" || exit 1"]
# tini reaps the processes that hddfancontrol starts (hdparm, smartctl) and
# passes SIGTERM on, so the fans are given back when the container stops.
ENTRYPOINT ["/sbin/tini", "--"]
CMD ["python3", "-m", "hddfancontrol_webui"]
