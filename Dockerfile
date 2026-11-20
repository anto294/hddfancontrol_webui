# syntax=docker/dockerfile:1

# Versions of what the image is built from. The upstream versions are
# checked weekly by .github/workflows/upstream.yml, the base images by
# Dependabot. The base images are pinned by the digest of their multi-arch
# index: a tag moved upstream cannot change the build. Written out in each
# FROM (no ARG), so that Dependabot can update them.
# Each tag with the commit it must point to: a tag moved upstream fails the build.
ARG HDDFANCONTROL_VERSION=2.1.2
ARG HDDFANCONTROL_COMMIT=4a6de520a71d9f6e2f7434287fe5d0b28e734ff4
ARG HDIDLE_VERSION=v1.21
ARG HDIDLE_COMMIT=8f720ada2f202327495ff07948b18232259eb0a3
ARG SDPARM_VERSION=1.12
ARG SDPARM_SHA256=c4c9efafdbeb662e2f9712707ec490932bd4d010bb1129ae7a99526546eeadbe

# --- hddfancontrol, static, so the same file also ships in the bare metal tarball
FROM alpine:3.24@sha256:f59abe656b692d901e184be3445e084786602108599d2a51d229c0f5a48c71bf AS hddfancontrol
ARG HDDFANCONTROL_VERSION
ARG HDDFANCONTROL_COMMIT
# A failing command inside a pipe must fail the build.
SHELL ["/bin/ash", "-eo", "pipefail", "-c"]
RUN apk add --no-cache cargo git musl-dev
WORKDIR /src
RUN git clone --depth 1 --branch "${HDDFANCONTROL_VERSION}" https://github.com/desbma/hddfancontrol.git . \
 && test "$(git rev-parse HEAD)" = "${HDDFANCONTROL_COMMIT}"
# With an explicit --target, RUSTFLAGS reaches only the program: the proc
# macros and build scripts are built for the host and cannot be static.
ENV RUSTFLAGS="-C target-feature=+crt-static" \
    CARGO_PROFILE_RELEASE_STRIP=true
RUN target="$(rustc -vV | sed -n 's/^host: //p')" \
 && cargo build --release --locked --target "${target}" \
 && install -D "target/${target}/release/hddfancontrol" /out/hddfancontrol

# --- hd-idle, cross-compiled: Go needs no emulation for other architectures
FROM --platform=$BUILDPLATFORM golang:1.27-alpine3.24@sha256:70bd7ddea8b6cbe5d6943cde279d6b67feb10cc924fb9100d1d8757c1575b280 AS hdidle
ARG HDIDLE_VERSION
ARG HDIDLE_COMMIT
ARG TARGETOS
ARG TARGETARCH
ARG TARGETVARIANT
RUN apk add --no-cache git
WORKDIR /src
RUN git clone --depth 1 --branch "${HDIDLE_VERSION}" https://github.com/adelolmo/hd-idle.git . \
 && test "$(git rev-parse HEAD)" = "${HDIDLE_COMMIT}" \
 && CGO_ENABLED=0 GOOS="${TARGETOS}" GOARCH="${TARGETARCH}" GOARM="${TARGETVARIANT#v}" \
    go build -trimpath -ldflags="-s -w" -o /out/hd-idle .

# --- sdparm: hddfancontrol uses it for the state of SAS drives. Alpine has it
# only in edge/testing, so it is built from the Debian copy of the release.
FROM alpine:3.24@sha256:f59abe656b692d901e184be3445e084786602108599d2a51d229c0f5a48c71bf AS sdparm
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
FROM alpine:3.24@sha256:f59abe656b692d901e184be3445e084786602108599d2a51d229c0f5a48c71bf
ARG APP_VERSION=dev
# python3 and aiohttp come built from Alpine for every architecture: no
# compiler, no pip, nothing downloaded at start.
RUN apk add --no-cache \
      hdparm \
      ipmitool \
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
      org.opencontainers.image.description="Fan control from drive temperatures and drive spin down, with a web UI, for any Linux host (TrueNAS SCALE, Proxmox VE, Unraid, OpenMediaVault…)" \
      org.opencontainers.image.source="https://github.com/anto294/hddfancontrol_webui" \
      org.opencontainers.image.licenses="GPL-3.0-or-later" \
      org.opencontainers.image.version="${APP_VERSION}"
ENV APP_VERSION=${APP_VERSION} \
    CONFIG_DIR=/config \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app
VOLUME /config
EXPOSE 28462
HEALTHCHECK --interval=60s --timeout=5s --start-period=60s \
  CMD ["/bin/sh", "-c", "wget -q -O /dev/null \"http://127.0.0.1:${WEBUI_PORT:-28462}/healthz\" || exit 1"]
# tini reaps the processes that hddfancontrol starts (hdparm, smartctl) and
# passes SIGTERM on, so the fans are given back when the container stops.
ENTRYPOINT ["/sbin/tini", "--"]
CMD ["python3", "-m", "hddfancontrol_webui"]
