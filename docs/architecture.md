# Architecture

How the web UI is put together: its processes, who may do what, where it keeps its
data and in which order things start and stop. The reasons behind these choices are
in [decisions.md](decisions.md).

## Processes

```
browser ──HTTPS/HTTP──▶ gateway (unprivileged)  ──Unix socket──▶ core (root)
                        login, CSRF, TLS,                         settings, fans,
                        static files                              daemons, history
                                                                     │
                                       ┌─────────────────────────────┼──────────────┐
                                       ▼                             ▼              ▼
                                 hddfancontrol                    hd-idle      smartctl, ipmitool,
                                 (measures only)                (spin down)    sensors-detect
```

- **Core** (`__main__.py`, `web.py`), root. It builds everything from the environment,
  writes the PWM files, starts the daemons and keeps the history. Its API listens on a
  **Unix socket only** (folder mode 0710, socket 0660 for the gateway's group), never
  on the network. `core_guard` checks the peer of each connection (`SO_PEERCRED`):
  only the gateway's user and root may talk to it. Every request that changes
  something is written to the audit log (`[audit]`).
- **Gateway** (`gateway.py`), the user `hddfancontrol-webui` (`WEBUI_USER`). It is the
  only process the network reaches: it serves the pages, checks the login, CSRF, the
  `Host` header and the client network, speaks TLS, and forwards `/api/…` and
  `/metrics` to the core. It runs with `python -I`, a filtered environment and no
  writable home. See [security.md](security.md).
- **hddfancontrol** measures the drive temperatures without waking sleeping drives and
  writes them to a JSON lines log. It never touches a fan: it always runs with
  `--fan-cmd /bin/true`. See [fan-control.md](fan-control.md).
- **hd-idle** spins down the drives that have a spin down time.
- Short-lived programs: `smartctl` (SMART data and self-tests), `sensors-detect` and
  `modprobe` (sensor modules at start), `ipmitool` (the BMC of a server,
  experimental, [bmc.md](bmc.md)), `openssl` (the self-signed certificate, in the
  gateway).

Without a usable gateway user (as root: a missing user, root itself or root's group),
there is no gateway and no web UI, but the core still controls the fans. It never
falls back to `nobody`.

Every program is started with `asyncio.create_subprocess_exec`, never through a shell,
and every value that reaches a command line has been checked by `settings.from_dict`.

## Inside the core

| Module | Job |
| --- | --- |
| `supervisor.py` | Runs hddfancontrol and hd-idle (`Daemon`: restart with a growing delay, 5 s doubling to 60 s), owns the fan controller, manual control and the PWM test behind one lock |
| `fancontrol.py` | Writes the PWM files: curve and auto modes, manual control, the safety rules |
| `templog.py` | Reads the last line of hddfancontrol's temperature log |
| `hardware.py` | Reads `/dev/disk/by-id`, `/sys/class/hwmon`, `/sys/class/block`, `/proc`; never sends a command to a drive; the host report of the hardware support request |
| `settings.py` | The settings and their only validation |
| `monitor.py`, `history.py` | Records temperatures and fan speeds, reads SMART, raises alerts |
| `alerts.py`, `notify.py` | Alert states, notes, quiet hours, sending to ntfy, Gotify or JSON |
| `smart.py`, `drivedb.py` | SMART data and self-tests; brand and recording type of a model |
| `cron.py` | The cron expressions of every schedule |
| `bmc.py` | EXPERIMENTAL: the iDRAC or iLO of a server |
| `labels.py`, `bays.py` | Names of drives and fans, the bay map |
| `privsep.py`, `tls.py`, `auth.py` | The gateway's user and folders, TLS, credentials |

Nothing blocks the event loop: file and SQLite work goes through
`asyncio.to_thread`, programs through `asyncio.create_subprocess_exec`.

## Where the data lives

Everything is in `/config` (`CONFIG_DIR`), written with mode 0600 through
`storage.write_atomic` (temporary file, fsync, rename, fsync of the folder), so a power
cut never leaves an empty file. The core runs with umask 077.

| Path | Owner | What |
| --- | --- | --- |
| `settings.json` | core | The settings; wins over the environment |
| `labels.json`, `bays.json` | core | Names of drives and fans, the bay map |
| `bmc.json` | core | EXPERIMENTAL: who drives the fans of a server and how |
| `pwm-state.json` | core | The original mode of each PWM output, with the boot id |
| `history.db` | core | SQLite (WAL): samples, SMART summaries, alert notes, `meta` |
| `templog/temps.jsonl` | hddfancontrol | Its temperature log, rotated daily |
| `web/` | gateway | `auth.json` (scrypt), the self-signed certificate |
| `web-tls/` | core | Copies of `TLS_CERT`/`TLS_KEY`, readable by the gateway's group |

Root never writes in or follows anything inside `web/`: a gateway taken over could
plant links there. At start, `tighten_config` closes older files of `/config`, and a
`/config` the gateway could write is closed first (`_close_config`, the sticky bit
does not count); when it cannot be closed, no gateway runs (see
[security.md](security.md)).

## Settings

`settings.json` wins over the environment variables, which keep working for a
container set up the upstream way (`env_document`). A broken file falls back to the
environment and shows an error. Applying settings restarts hddfancontrol and hd-idle;
names, bays and the BMC configuration have their own files and never restart
anything.

The PWM outputs are kept with the `/sys/devices/…` path of their chip. When the hwmon
numbers change at a reboot (`hwmon2` becomes `hwmon3`), `Supervisor._placed` finds the
same output again (`Hardware.locate`), saves the settings and moves the label. A fan
whose chip is gone is listed in `missing_pwms` and only that fan is not controlled: the
others are, and hddfancontrol measures the drives all the same. While the list is not
empty the supervisor looks again every `PLACE_PERIOD` (60 s) and after each sensor scan
of the page (`recheck_fans`), and restarts with the fan as soon as its chip is back. The
chip of a fan is always the host's: `apply` drops one sent with the settings, and
`_placed` finds the real one with `Hardware.chip_of`. Saving at start and
moving the label are extras: when `/config` is full or read only, the error is logged
(`ERROR: …`) and the fans are controlled at their new place anyway. An apply still
fails with `save_failed` when the settings cannot be saved (that is what was asked),
but never because of a label.

## Start and stop

Start, in the background so the web UI answers at once:

1. The BMC, if any: a Dell gets its fans back before anything may take them.
2. The sensor modules (`sensors-detect`), unless `LOAD_SENSOR_MODULES=false`. A failure
   never keeps the fans from being controlled. A scan always ends as `done`, at start
   and when the page asks for one again (`POST /api/sensors/scan`): a program that
   hangs is killed at `RUN_TIMEOUT` (300 s) and reported, and an unexpected error
   lands in `detect_error`, so a later scan is never refused as `busy`. The page's scan
   answers 202 at once and runs in the background (longer than the gateway's
   `CORE_TIMEOUT_S`); the page follows `sensors.state` in the status and reads the
   hardware again when it is `done`.
3. `Supervisor.start`: hddfancontrol, hd-idle, the fan controller.
4. `Monitor.start`: recording, SMART, alerts.

Stop (SIGTERM or SIGINT):

1. `Supervisor.release_fans`: cancels a PWM test, ends manual control, gives every fan
   back (`fans.stop(final=True)`); from then on nothing starts and changes answer
   `Busy`.
2. The BMC gets its fans back.
3. The gateway stops, then the daemons.

## Read only

When `/sys` is mounted read only (a container that is not privileged), nothing starts:
the mode is `read_only`, temperatures and SMART still show, and manual control and the
PWM test are refused.
