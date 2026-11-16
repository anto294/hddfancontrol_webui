<p align="center"><img src="img/logo.png" alt="" width="160"></p>

# hddfancontrol web UI

[![GitHub release](https://img.shields.io/github/v/release/anto294/hddfancontrol_webui?style=plastic)](https://github.com/anto294/hddfancontrol_webui/releases)
[![CI](https://img.shields.io/github/actions/workflow/status/anto294/hddfancontrol_webui/ci.yml?style=plastic&label=CI)](https://github.com/anto294/hddfancontrol_webui/actions/workflows/ci.yml)
[![Docker image](https://img.shields.io/badge/ghcr.io-hddfancontrol__webui-2f8cff?style=plastic&logo=docker&logoColor=white)](https://github.com/anto294/hddfancontrol_webui/pkgs/container/hddfancontrol_webui)
[![License](https://img.shields.io/github/license/anto294/hddfancontrol_webui?style=plastic)](LICENSE)

Control the case fans of a NAS from the temperature of its hard drives, and spin the drives down when they are idle, from a web page. Built around [hddfancontrol](https://github.com/desbma/hddfancontrol) by desbma and [hd-idle](https://github.com/adelolmo/hd-idle) by adelolmo, for **TrueNAS SCALE** first, and for any Linux system with Docker or systemd.

> [!NOTE]
> This project started from the idea of [fightforlife/docker_hddfancontrol](https://github.com/fightforlife/docker_hddfancontrol), a Docker image of the same two programs. It is written from scratch, and its environment variables still work, so moving over is a change of image. See [Moving from fightforlife/docker_hddfancontrol](#moving-from-fightforlifedocker_hddfancontrol).

## Contents

- [What you can do with it](#what-you-can-do-with-it)
- [Requirements](#requirements)
- [Installation](#installation)
- [First setup](#first-setup)
- [Fan control modes](#fan-control-modes)
- [Spin down](#spin-down)
- [History, SMART data and names](#history-smart-data-and-names)
- [Security](#security)
- [Environment variables](#environment-variables)
- [Moving from fightforlife/docker_hddfancontrol](#moving-from-fightforlifedocker_hddfancontrol)
- [Resource use](#resource-use)
- [Troubleshooting](#troubleshooting)
- [Development](#development)
- [Credits and license](#credits-and-license)

## What you can do with it

- **Keep the drives cool and the NAS quiet:** the fans follow the hottest drive, either along a curve between two temperatures, or in the auto mode, which keeps the drives at a target with as little fan speed as it needs.
- **Find out which fan is which:** move a slider for each PWM output and watch the speed change.
- **Set up the fans without a shell:** the PWM test finds the value at which each fan starts and stops, and copies them into the settings.
- **Let the drives sleep:** hd-idle spins them down after an idle time, which you can set per drive. Nothing in the web UI wakes a sleeping drive.
- **See what happened:** temperatures, fan speeds and PWM are kept in a small database, with charts from one hour to a year.
- **Keep an eye on the drives:** SMART health, power-on hours, reallocated and pending sectors, CRC errors, NVMe wear.
- **Name things:** "Front fan", "Parity drive" instead of `pwm2` and `sdc`.
- In English and Italian, with a light and a dark theme, on a phone as well.

## Requirements

- A mainboard whose fan headers are PWM-controlled through a Linux hwmon driver (`nct6775`, `it87`, …). Most consumer and many server boards work; if a BIOS fan mode exists, the fans must not be locked at full speed.
- **Docker:** any x86_64, arm64 or armv7 host. TrueNAS SCALE 24.10 or later runs it as a Custom App.
- **Bare metal:** Linux with systemd and Python 3.11 or later (Debian 12, Ubuntu 24.04, Proxmox VE 8, OpenMediaVault 7, Fedora, Arch, …), plus `smartmontools` and `hdparm`.

## Installation

### TrueNAS SCALE

1. Create a dataset for the settings, for example `apps/hddfancontrol`.
2. Go to **Apps → Discover Apps → ⋮ → Install via YAML**, name the app `hddfancontrol`, and paste [docker-compose.yml](docker-compose.yml) with the `./config` volume changed to your dataset (`/mnt/tank/apps/hddfancontrol:/config`) and a password of your own.
3. Open `http://<truenas>:8080`.

If the web UI says it cannot load a kernel module (for example `nct6775`), TrueNAS has the module but the container cannot load it. Load it on the host once:

1. **System → Advanced Settings → Init/Shutdown Scripts → Add**
2. Type **Command**, Command `modprobe nct6775`, When **Post Init**, then **Save**.
3. Run `sudo modprobe nct6775` once in **System → Shell** (or reboot), and press **Scan again** in the web UI.

### Docker Compose

```bash
mkdir hddfancontrol && cd hddfancontrol
curl -fsSLO https://raw.githubusercontent.com/anto294/hddfancontrol_webui/main/docker-compose.yml
# change WEBUI_PASSWORD, then:
docker compose up -d
```

The image is `ghcr.io/anto294/hddfancontrol_webui`, with these tags:

| Tag | What it is |
|---|---|
| `latest` | The latest stable release |
| `X.Y` / `vX.Y.Z` | A given release |
| `edge` | The `main` branch, built at every change |

The container runs privileged: it writes the PWM files of `/sys` and sends ATA commands to the drives. The root file system can be read only; everything it keeps is in `/config`.

### Bare metal

Download `hddfancontrol-webui-<version>-linux-<arch>.tar.gz` from the [latest release](https://github.com/anto294/hddfancontrol_webui/releases/latest) (`amd64`, `arm64` or `armv7`), then:

```bash
tar xzf hddfancontrol-webui-*-linux-amd64.tar.gz
cd hddfancontrol-webui-*-linux-amd64
sudo ./install.sh
```

The tarball carries static builds of hddfancontrol 2, hd-idle and sdparm, so the version of your distribution does not matter (Debian still ships hddfancontrol 1, whose options differ). `install.sh` puts everything in `/opt/hddfancontrol-webui`, the settings in `/etc/hddfancontrol-webui/env`, the data in `/var/lib/hddfancontrol-webui`, and starts the `hddfancontrol-webui` service. Run it again from a newer tarball to update.

- Logs: `journalctl -u hddfancontrol-webui`
- Remove, keeping settings and history: `sudo ./install.sh --uninstall`
- Remove everything: `sudo ./install.sh --purge`

On bare metal, `sensors-detect --auto` does not run at start, because on a real host it can change `/etc/modules`. Run `sudo sensors-detect` once instead, or set `LOAD_SENSOR_MODULES=true` in `/etc/hddfancontrol-webui/env`.

## First setup

1. **Security:** set a username and a password, in the compose file or in the **Security** tab.
2. **Fans:** check that the **Fans** tab lists your fan chip and its PWM outputs. If it does not, see [Troubleshooting](#troubleshooting).
3. **PWM test:** in **PWM test**, tick the outputs with a fan and start the test. Each fan stops and starts several times, for a few minutes per fan, then **Use these values in the settings** copies what it found.
4. **Settings:** tick the drives and the fans, choose a mode and its temperatures, and **Save and apply**.

## Fan control modes

| Mode | What drives the fans | You set |
|---|---|---|
| **Curve** | hddfancontrol, as in the upstream image: minimum speed up to the minimum temperature, full speed from the maximum, in proportion in between | Minimum and maximum temperature, minimum speed |
| **Auto** | The web UI keeps the hottest drive near a target with as little speed as it needs, and adapts to load, season and case by itself | Target and maximum temperature, minimum speed |
| **Manual** | You, with a slider per PWM output, until you go back or the container restarts | — |

The auto mode is a PI controller (feedback control, not machine learning). It speeds up at once when the drives warm, and slows down at most 10 % per check, so a short dip does not stop the fans. In both the auto mode and manual control, hddfancontrol keeps running to read the drive temperatures without waking sleeping drives, and two safety rules apply: a drive at the maximum temperature puts every fan at full speed, and in the auto mode, temperatures that stop coming in do too. When control ends, every PWM output gets its previous mode back.

## Spin down

hd-idle spins the chosen drives down after an idle time: one for every drive, and, in the drive list of **Settings**, one per drive, where 0 keeps that drive spinning. Drives that are not chosen are never spun down. Use `scsi` as the command type for SAS drives and for USB enclosures that do not pass ATA commands.

## History, SMART data and names

- **History:** every 30 seconds the web UI stores the drive temperatures that hddfancontrol measured, and the PWM and speed of every fan, in `/config/history.db` (SQLite). It keeps 30 days by default; longer windows are averaged, so a chart never has more than 600 points per line.
- **SMART:** `smartctl -a -n standby` reads every drive every 6 hours by default, and when you press **Read SMART now**. A drive asleep at that time is not woken and keeps its last data.
- **Names:** give a fan or a drive a name in the **Fans** tab or the drive list of **Settings**. Names are kept in `/config/labels.json`, and changing one does not restart fan control. Drives are named by their `/dev/disk/by-id` path, which does not change between boots.

## Security

- Set a password. Without one, anyone who reaches the port can stop the fans; the web UI says so at the top of every page.
- Credentials set in the web UI are stored as a scrypt hash in `/config/auth.json`. `WEBUI_USERNAME` and `WEBUI_PASSWORD` win over them, and cannot be changed from the web UI.
- A login gives an HttpOnly, SameSite=Strict session cookie, valid for 7 days or until the container restarts. Scripts can use HTTP Basic authentication. A failed login waits one second.
- Every change needs a header that other sites cannot send, and every value is checked before it reaches a command line or a file: only `/dev` paths for drives, only PWM files that exist on the host for manual control and the PWM test.
- The web UI has no HTTPS of its own. To reach it from outside your network, put it behind a reverse proxy with HTTPS, or a VPN.

## Environment variables

Everything but the first group can be set in the web UI, which then stores it in `/config/settings.json`. From then on the file wins; **Back to environment variables** deletes it.

| Variable | Default | Meaning |
|---|---|---|
| `WEBUI_USERNAME` | `admin` | Username, when `WEBUI_PASSWORD` is set |
| `WEBUI_PASSWORD` | — | Password of the web UI |
| `WEBUI_PORT` | `8080` | Port of the web UI |
| `WEBUI_HOST` | `0.0.0.0` | Address to listen on |
| `CONFIG_DIR` | `/config` | Where settings, names, history and credentials are kept |
| `LOAD_SENSOR_MODULES` | `true` (`false` bare metal) | Run `sensors-detect --auto` at start and load the chip modules it finds |
| `HDDFANCONTROL_BIN`, `HDIDLE_BIN`, `SMARTCTL_BIN`, `SENSORS_DETECT_BIN`, `MODPROBE_BIN` | from `PATH` | Programs to use |
| `DRIVES` | — | Drives, separated by spaces: `/dev/disk/by-id/...` paths or interface names (`ata`) |
| `PWM` | — | Fans, as `PWM_PATH:START:STOP`, separated by spaces |
| `CONTROL_MODE` | `curve` | `curve` or `auto` |
| `DRIVE_TEMP_RANGE` | `30 50` | Minimum and maximum temperature, °C |
| `TARGET_TEMP` | `40` | Target temperature of the auto mode, °C |
| `MIN_FAN_SPEED_PRCT` | `20` | Minimum fan speed, % |
| `INTERVAL` | `20s` | Check interval (`10s`, `1min`, …) |
| `AVERAGE` | `1` | Number of samples to average |
| `HWMONS` | — | Other temperature inputs, as `PATH[:MIN:MAX]`, separated by spaces |
| `RESTORE_FAN_SETTINGS` | `false` | When hddfancontrol stops, give the fans back their settings instead of full speed |
| `VERBOSITY` | `INFO` | `TRACE`, `DEBUG`, `INFO`, `WARN` or `ERROR` |
| `SPIN_DOWN_TIME_S` | `600` | Idle time before spin down, seconds; `0` turns hd-idle off |
| `SPIN_DOWN_COMMAND` | `ata` | `ata` or `scsi` |
| `HISTORY_DAYS` | `30` | Days of history to keep |
| `SMART_INTERVAL_H` | `6` | Hours between SMART reads; `0` reads only on demand |

## Moving from fightforlife/docker_hddfancontrol

Change the image to `ghcr.io/anto294/hddfancontrol_webui:latest`, add a `/config` volume and port 8080, and keep your environment variables. Three things behave differently:

- **hd-idle spins down only the drives in `DRIVES`.** The upstream script set the idle time for every drive of the system.
- **`RESTORE_FAN_SETTINGS=false` means false.** Upstream turned the option on whatever its value.
- **A daemon that stops is started again**, with a growing delay, instead of the whole container stopping. Without `PWM`, the web UI waits for you to set the fans up, instead of waiting for a shell.

## Resource use

The web UI is a single Python process with aiohttp, and uses about 45 MB of memory; hddfancontrol and hd-idle add a few MB. The browser asks for data only while its tab is visible, the temperature log is read from its end only, and the history is written every 30 seconds in one transaction. The image is based on Alpine, with Python and aiohttp from Alpine's own packages.

## Troubleshooting

**The Fans tab shows no PWM output.** The kernel module of the fan chip is not loaded. The web UI shows which one it found and could not load; on TrueNAS, load it on the host as shown in [Installation](#truenas-scale). If `dmesg` then shows a resource conflict with ACPI, the chip also needs the kernel option `acpi_enforce_resources=lax`; on TrueNAS SCALE, run `midclt call system.advanced.update '{"kernel_extra_options": "acpi_enforce_resources=lax"}'` in the shell and reboot.

**A fan does not react to its slider.** The header may be in DC mode, or the BIOS may hold it at a fixed speed. Set the header to PWM mode in the BIOS.

**The hwmon number of the fan chip changed after a reboot.** Linux numbers hwmon devices in the order they appear, so `hwmon3` can become `hwmon4`. The Fans tab then shows the configured fans as not found: tick the new paths in **Settings**. Loading the module the same way at every boot keeps the order.

**A drive never sleeps.** Something reads or writes it: a pool scrub, SMART tests, an app. The web UI itself reads temperatures only through hddfancontrol, which leaves sleeping drives alone, and SMART data only with `-n standby`.

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e . pytest pytest-aiohttp pytest-asyncio pytest-cov black flake8 ruff mypy bandit
python -m pytest --cov
black . && ruff check hddfancontrol_webui --select E,F,B,ASYNC,S,I --ignore E501 && mypy
docker build -t hddfancontrol_webui:dev .
```

To try the web UI without touching the fans, point `HDDFANCONTROL_BIN` and `HDIDLE_BIN` at scripts that only sleep, and set `CONFIG_DIR` and `LOAD_SENSOR_MODULES=false`. See [CLAUDE.md](CLAUDE.md) for the architecture.

## Credits and license

- [hddfancontrol](https://github.com/desbma/hddfancontrol) by desbma (GPL-3.0) measures the drives and drives the fans.
- [hd-idle](https://github.com/adelolmo/hd-idle) by adelolmo (GPL-3.0) spins the drives down.
- [sdparm](https://sg.danny.cz/sg/sdparm.html) by Douglas Gilbert (BSD) reads the state of SAS drives.
- [fightforlife/docker_hddfancontrol](https://github.com/fightforlife/docker_hddfancontrol) gave the idea and the names of the environment variables.

This project is released under the [GNU General Public License v3.0](LICENSE). The image and the release tarballs include the programs above, under their own licenses.
