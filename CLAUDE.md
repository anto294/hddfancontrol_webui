# CLAUDE.md

Web UI and process supervisor for [hddfancontrol](https://github.com/desbma/hddfancontrol) 2.x and [hd-idle](https://github.com/adelolmo/hd-idle). Repository: `anto294/hddfancontrol_webui`. Shipped as a Docker image (`ghcr.io/anto294/hddfancontrol_webui`, main target TrueNAS SCALE) and as bare metal tarballs (systemd). It started from the idea of `fightforlife/docker_hddfancontrol`, which has no license: nothing is copied from it, only its environment variable names are kept. All code lives in `hddfancontrol_webui/`.

## Architecture

- **`__main__.py`**: builds everything from the environment and runs aiohttp. At start, in the background: sensor module scan (unless `LOAD_SENSOR_MODULES=false`), then `Supervisor.start()`, then `Monitor.start()`. SIGTERM stops the monitor, then the supervisor, which gives the fans back.
- **`settings.py`**: the `Settings` dataclass, `from_dict` (the only validation; collects every problem as `{field, code, params}`, codes translated as `error.<code>`), `env_document` (upstream variable names → document; `MONITOR_DRIVES` is ours, and without it `monitor_drives` defaults to the `/dev` paths of `drives`), `hddfancontrol_args` / `hdidle_args` (command lines), `SettingsStore` (`settings.json` wins over the environment; a bad file falls back to the environment and sets `error`). Every value that reaches a command line or a path goes through here: drives are `/dev/...` paths or interface names, PWM and hwmon files are `/sys/...` paths, no `..`.
- **`supervisor.py`**: `Daemon` runs one program and restarts it with a growing delay (5 s doubling to 60 s, reset after 60 s of running); stop sends SIGTERM, then SIGKILL after `STOP_TIMEOUT`. `Supervisor` owns hddfancontrol, hd-idle, the `FanController`, manual mode and the PWM test, behind one `asyncio.Lock`; changes are refused with `Busy` during a PWM test. Manual control ends by itself after `MANUAL_IDLE_TIMEOUT` (120 s) without `touch_manual()`, which `GET /api/status` (polled by every open page) and `set_pwm` call. With `read_only` (from `Hardware.read_only()`: `/sys` mounted read only, as in a container that is not privileged) nothing starts, mode is `read_only`, and manual control and the PWM test raise `ReadOnly`. In manual mode `set_pwm` takes any PWM output of the host (to find out which fan it drives); the curve and auto modes write only `settings.pwms`. `LogBuffer` keeps the last lines and echoes them to stdout as `[source] text`.
- **Modes**: `curve` → hddfancontrol with `--pwm`. `auto` and manual → hddfancontrol with `--fan-cmd /bin/true`, so it only probes the drives (without waking them) and writes `--temp-log`; `fancontrol.py` writes the PWM files. Manual mode is never saved.
- **`fancontrol.py`**: `PwmWriter` saves `pwmN_enable` and the value the first time it writes an output, sets enable to 1, and `restore()` puts them back. `PiController` (KP, KI, at most -10 % per step, full speed at `drive_temp_max`). `FanController` runs the auto loop or the manual safety loop. Safety rules that must never be weakened: a drive at `drive_temp_max` → every fan at 255; no sample newer than `max(3 × interval, 120 s)` → full speed (auto mode; manual mode too when drives are set, with `safety_reason` "stale"); `stop()` always restores.
- **`templog.py`**: reads only the last line of hddfancontrol's JSONL temp log, when its size or mtime changed, and keeps the last sample across the daily rotation. hddfancontrol logs every drive first (an interface name such as `ata` gives one entry per drive), then every hwmon input, which is never missing: `_hottest` takes every value but the last `len(hwmons)`.
- **`hardware.py`**: reads `/dev/disk/by-id` and `/sys/class/hwmon` (and `/sys/class/block`). Returns real `/sys/...` paths even when tests read a fake tree. Never sends a command to a drive.
- **`history.py`**: SQLite (`/config/history.db`, WAL) with `series(kind, name)` and `samples(series, ts, value)`; `query` averages into at most `HISTORY_MAX_POINTS` buckets per series. Also keeps the last SMART summary per drive. Blocking: call it with `asyncio.to_thread`.
- **`monitor.py`**: records the newest temperature sample (once per sample, each drive under a stable by-id name, `wwn-` first, never under `sdX`) and PWM/RPM of every output every `RECORD_PERIOD`, logs a `[temps]` summary line (`summary`, on change or every `TEMPS_HEARTBEAT`), purges hourly, reads SMART of `settings.monitor_drives` (only those, and `Monitor.smart` shows only those) every `smart_interval_h` hours. A drive asleep at a SMART refresh keeps its previous data with `asleep: true`.
- **`smart.py`**: `smartctl -j -a -n standby`, reduced by `summarize` to the fields the UI shows plus the attribute table. Warnings are codes translated as `smart.warn.<code>`.
- **`sensors.py`**: runs `sensors-detect --auto`, takes only well-formed `modprobe <name>` lines of the "Chip drivers" section, skips modules present in `/sys/module`, runs `modprobe` without a shell. Failures are reported in `SensorReport` and shown in the UI with the TrueNAS Init Script steps.
- **`auth.py`**: credentials from `WEBUI_USERNAME`/`WEBUI_PASSWORD` (win, read only) or `/config/auth.json` (scrypt, mode 0600). In-memory sessions, 7 days. Changing or removing stored credentials needs the current password and ends every session.
- **`aio.py`**: `cancel_and_wait(task)` cancels a task and waits for it; the task's own cancellation is swallowed, its error and a cancellation of the caller are not. Use it instead of `with suppress(CancelledError): await task`.
- **`storage.py`**: `write_atomic` writes every file of `/config` (settings, labels, auth) with mode 0600 to a temporary file, fsyncs it, renames it and fsyncs the folder, so a power cut never leaves an empty file. Blocking.
- **Fan chips**: `Pwm.chip` is the `/sys/devices/...` path of the chip (`Hardware.chip_of`). `Supervisor._placed` runs at start, apply and reset: it records the chip, finds the file again with `Hardware.locate` when the hwmon number changed (saving the settings and moving the label), and lists fans whose chip is gone in `missing_pwms`, which are then not controlled.
- **`labels.py`**: names of drives (by `/dev/disk/by-id` path) and fans (by PWM path) in `/config/labels.json`. Never restarts anything.
- **`web.py`**: middlewares in this order: security headers (strict CSP, no inline script or style), authentication (public: login page, its static files, `/healthz`), CSRF (non-GET needs `X-Requested-With: fetch` and a matching `Origin`). Errors are `{error: code, problems: [...]}`.
- **`static/`**: no build step and no external resources. `icons.js` holds the line icons, built as SVG nodes (`icon(name)`, or `data-icon` on an element); bars and charts are SVG with attributes, never style attributes (CSP). `wizard.js` is the guided setup: it builds a settings document from the answers and saves it through `PUT /api/settings` like the Settings tab; it opens by itself while the settings come from the environment with no drive and no fan, until it is closed once (`localStorage` `setupSkipped`). `app.js` builds the DOM with `el()` and `textContent` only — never `innerHTML` with server data. Texts are flat keys in `static/i18n/en.json` and `it.json`; `tests/test_translations.py` fails if a key, a placeholder, a server error code or a status lacks a text.

## Conventions

- One logical change per commit, with a detailed English message (summary, then what and why). Never batch unrelated changes. Run black before committing.
- Format with **black** (line length 127, `pyproject.toml`). CI runs `black . --check`.
- Types: CI runs `mypy` strict (`setup.cfg`), with Python 3.11 as the target. A `type: ignore` needs a comment that says why.
- Lint: CI runs flake8 (errors only), and ruff with `E,F,B,ASYNC,S,I` on the package and `E,F,B,ASYNC,I` on the tests; a finding fails the job. Bandit and CodeQL run too. shellcheck checks `packaging/install.sh`, hadolint the Dockerfile, actionlint the workflows.
- Tests: pytest with `asyncio_mode = auto`; coverage must stay at 95 % or more. Tests use the fake `/sys` and `/dev` trees and fake programs of `tests/conftest.py`; never touch the real fans or drives.
- Every function and class gets a docstring, and top-level ones get this banner:
  ```python
  # ---------------------------
  #   function_name
  # ---------------------------
  ```
- Never block the event loop: file and SQLite work goes through `asyncio.to_thread`, programs through `asyncio.create_subprocess_exec` (never a shell).
- Keep the resource use low: no polling while the browser tab is hidden, no full reads of growing files, no new long-running process without a reason.
- Supported Python: 3.11 or later for bare metal (CI tests 3.11 and 3.14); the image uses Alpine's `python3` and `py3-aiohttp`. Do not add a dependency that Alpine does not package.
- The upstream versions are pinned in the Dockerfile (`HDDFANCONTROL_VERSION`, `HDIDLE_VERSION`, `SDPARM_VERSION` with its SHA-256). `upstream.yml` opens an issue when a new release is out; on an update, check that `hddfancontrol_args` still matches its command line and that the `pwm-test` output still matches `_THRESHOLDS` in `supervisor.py`.
- `pyproject.toml` keeps `version = "0.0.0"`. The release workflow sets the real version for the wheel, and `docker.yml` passes the tag as `APP_VERSION`.
- Versions follow Semantic Versioning: a release with only fixes raises the patch number, one that adds a function or changes a default raises the minor number. Tags are `vX.Y.Z` or `vX.Y.ZbN` (the workflows reject anything else); publish a full GitHub release, not just a tag, and mark betas as pre-releases. Every user-facing change gets an entry under `## [Unreleased]` in `CHANGELOG.md` (Keep a Changelog); the release notes are that version's section, passed with `gh release create --notes-file`.
- The default branch is `main`.

## Development commands

```bash
pip install -e . pytest pytest-aiohttp pytest-asyncio pytest-cov black flake8 ruff mypy bandit
black .
ruff check hddfancontrol_webui --select E,F,B,ASYNC,S,I --ignore E501
mypy
python -m pytest --cov --cov-report=term-missing
docker build -t hddfancontrol_webui:dev .
```

To run the web UI without touching the fans: `CONFIG_DIR=/tmp/hfc LOAD_SENSOR_MODULES=false HDDFANCONTROL_BIN=<script> HDIDLE_BIN=<script> python -m hddfancontrol_webui`, with scripts that only sleep (or append fake samples to the `--temp-log` path they are given).
