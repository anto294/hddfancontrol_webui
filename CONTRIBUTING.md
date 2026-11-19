# Contributing

Bug reports, hardware reports, [hardware support requests](https://github.com/anto294/hddfancontrol_webui/issues/new?template=hardware_support.yml), translations and pull requests are welcome. For a larger change, open an issue first, so that we can agree on the approach before you write the code.

## Set up

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e . pytest pytest-aiohttp pytest-asyncio pytest-cov black flake8 ruff mypy bandit
```

Python 3.11 or later. The image uses Alpine's own Python packages, so do not add a dependency that Alpine does not package.

## Check your change

CI runs all of these, and a finding fails the job:

```bash
black .
ruff check hddfancontrol_webui --select E,F,B,ASYNC,S,I --ignore E501
mypy
python -m pytest --cov --cov-report=term-missing
docker build -t hddfancontrol_webui:dev .
```

- **black** with line length 127, **mypy** strict.
- **Tests** run against a fake `/sys` and `/dev` and fake programs (`tests/conftest.py`); they never touch real fans or drives. Coverage must stay at 95 % or more.
- **Translations:** every text of the web UI is a key in each file of `hddfancontrol_webui/static/i18n/`; `tests/test_translations.py` fails when one is missing.

## Try the web UI without touching the fans

Point `HDDFANCONTROL_BIN` and `HDIDLE_BIN` at scripts that only sleep (or that append fake samples to the `--temp-log` file they are given), and run:

```bash
CONFIG_DIR=/tmp/hfc LOAD_SENSOR_MODULES=false HDDFANCONTROL_BIN=<script> HDIDLE_BIN=<script> python -m hddfancontrol_webui
```

## Commits and pull requests

- One logical change per commit, with an English message: a summary line, then what changed and why.
- Every user-facing change gets an entry under `## [Unreleased]` in [CHANGELOG.md](CHANGELOG.md).
- Every function and class has a docstring. File and SQLite work goes through `asyncio.to_thread`, programs through `asyncio.create_subprocess_exec`, never a shell.
- The safety rules of fan control (full speed on a hot drive, on missing temperatures and on errors; the fans given back at the end) must never be weakened.

[CLAUDE.md](CLAUDE.md) describes the architecture, module by module, and the full conventions.

## Security

Do not open a public issue for a vulnerability: see [SECURITY.md](SECURITY.md).
