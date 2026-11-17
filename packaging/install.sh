#!/bin/sh
# Install, update or remove hddfancontrol-webui on a Linux host with systemd.
#
#   sudo ./install.sh              install or update, then start the service
#   sudo ./install.sh --no-start   install or update without starting it
#   sudo ./install.sh --uninstall  remove the program, keep settings and history
#   sudo ./install.sh --purge      remove everything, settings and history too
#
# Run it from the extracted release tarball.
set -eu

PREFIX=/opt/hddfancontrol-webui
ETC=/etc/hddfancontrol-webui
DATA=/var/lib/hddfancontrol-webui
UNIT=/etc/systemd/system/hddfancontrol-webui.service
HERE=$(cd "$(dirname "$0")" && pwd)

say() { printf '%s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run as root (sudo $0 $*)"
command -v systemctl >/dev/null 2>&1 || die "systemd is needed"

uninstall() {
    systemctl disable --now hddfancontrol-webui.service 2>/dev/null || true
    rm -f "$UNIT"
    systemctl daemon-reload
    rm -rf "$PREFIX"
    userdel hddfancontrol-webui 2>/dev/null || true
    say "Removed $PREFIX, the service and its user."
}

case "${1:-}" in
    --uninstall)
        uninstall
        say "Settings and history are kept in $ETC and $DATA."
        exit 0
        ;;
    --purge)
        uninstall
        rm -rf "$ETC" "$DATA"
        say "Removed $ETC and $DATA."
        exit 0
        ;;
    "" | --no-start) ;;
    *) die "unknown option: $1" ;;
esac

# Python 3.11 or later, with venv (python3-venv on Debian and Ubuntu).
command -v python3 >/dev/null 2>&1 || die "python3 is needed"
python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' || die "Python 3.11 or later is needed"
python3 -c 'import venv, ensurepip' 2>/dev/null || die "the venv module is needed (Debian/Ubuntu: apt install python3-venv)"

missing=""
for program in smartctl hdparm; do
    command -v "$program" >/dev/null 2>&1 || missing="$missing $program"
done
if [ -n "$missing" ]; then
    say "warning: missing:$missing"
    say "  Debian/Ubuntu: apt install smartmontools hdparm lm-sensors"
    say "  Arch:          pacman -S smartmontools hdparm lm_sensors"
    say "  Fedora:        dnf install smartmontools hdparm lm_sensors"
fi

wheel=$(find "$HERE" -maxdepth 1 -name "hddfancontrol_webui-*.whl" | head -n 1)
[ -n "$wheel" ] || die "no wheel next to install.sh: run it from the extracted release"

running=false
systemctl is-active --quiet hddfancontrol-webui.service && running=true
$running && systemctl stop hddfancontrol-webui.service

# The gateway (the part the network reaches) runs as this user, not root.
if ! getent passwd hddfancontrol-webui >/dev/null; then
    useradd --system --user-group --no-create-home --home-dir /nonexistent \
        --shell /usr/sbin/nologin hddfancontrol-webui \
        || say "warning: cannot create the user hddfancontrol-webui; the web server will run as nobody"
fi
install -d -m 0755 "$PREFIX/bin"
install -m 0755 "$HERE"/bin/hddfancontrol "$HERE"/bin/hd-idle "$HERE"/bin/sdparm "$PREFIX/bin/"
# A new venv each time, so an update never mixes old and new packages.
rm -rf "$PREFIX/venv"
python3 -m venv "$PREFIX/venv"
"$PREFIX/venv/bin/pip" install --quiet --disable-pip-version-check "$wheel"

install -d -m 0755 "$ETC"
[ -f "$ETC/env" ] || install -m 0600 "$HERE/env.example" "$ETC/env"
install -m 0644 "$HERE/hddfancontrol-webui.service" "$UNIT"
systemctl daemon-reload
systemctl enable hddfancontrol-webui.service >/dev/null

if [ "${1:-}" = "--no-start" ]; then
    say "Installed. Start it with: systemctl start hddfancontrol-webui"
else
    systemctl restart hddfancontrol-webui.service
    port=$(sed -n 's/^WEBUI_PORT=//p' "$ETC/env" | tail -n 1)
    say "Installed and started: http://$(hostname):${port:-8080}"
fi
say "Settings: $ETC/env   Data: $DATA   Logs: journalctl -u hddfancontrol-webui"
