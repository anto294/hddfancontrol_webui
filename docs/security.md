# Security design

How the web UI keeps the network away from root, in the order a request meets each
check. What a user sees and sets (networks, host names, login limits, headers) is
summed up in [SECURITY.md](../SECURITY.md); this page is for whoever changes the code.

## The trust boundary

The container must run privileged: the fans (`/sys`) and the drives (`/dev`) need it.
So the code is split where the network meets it:

- the **gateway** (`gateway.py`) holds the port and runs as an unprivileged user;
- the **core** (`web.py` and the rest) runs as root and listens only on a Unix socket.

A flaw reached before the login stays in a process that cannot touch the fans, the
drives or the rest of `/config`. This split must never be weakened: no new port on the
core, no root work triggered by a request the gateway did not authenticate, no code in
the gateway that needs root.

The gateway is started by the core through a `Daemon` with its user and group, a
filtered environment (`privsep.GATEWAY_ENV`: no `NOTIFY_URL`, for example),
`HOME=/nonexistent`, no `PYTHONPATH` and `python -I`, the package root passed as an
argument. It may write only `/config/web` (credentials, its self-signed certificate).
Root never writes in or follows anything inside that folder.

The rest of `/config` must be root's alone: the core trusts the settings, the fans'
state (`pwm-state.json`) and the temperature log it finds there. A `/config` the
gateway could write (its own, its group's, or everyone's, even with the sticky bit,
which protects only names that already exist) is given to root and closed at start
(`_close_config`). When the host refuses (NFS with `root_squash`, an ACL), there is no
gateway at all: the core drives the fans alone, as with a missing `WEBUI_USER`, and
the log says why. Even so, `PwmWriter` reads back from `pwm-state.json` only PWM
outputs (`is_pwm_path`) with numbers from 0 to 255, and checks the path again before
each write.

## A request, step by step

Before any middleware, the listener (`tls.DualListener`, with TLS on or off) closes a
connection from an address outside `WEBUI_ALLOW` at accept, before reading a byte or
making a handshake (`admit_address`, logged once an hour). It keeps at most
`MAX_CONNECTIONS_PER_ADDRESS` (64) open connections per address and `MAX_CONNECTIONS`
(512) in all, closing the rest at once, and drops a connection whose first request
(headers, and a body by its `Content-Length`) is not complete `REQUEST_TIMEOUT_S`
(30 s) after its first byte: aiohttp waits for headers for ever, so silent
connections could run the gateway out of file descriptors, `/healthz` included.

The gateway's middlewares run in this order:

1. **`network_filter`**: the client address must be in `WEBUI_ALLOW` (private, loopback,
   link-local and CGNAT networks by default); otherwise a bare 403, logged once an hour.
   The listener already refused such an address; this is the same rule again.
2. **`host_check`**: the `Host` header must be an IP address, `localhost`, a dotless
   name, a name under `LOCAL_SUFFIXES` (`.lan`, `.local`, `.home.arpa`, `.ts.net`…) or
   one of `WEBUI_HOSTS`. This stops DNS rebinding. `/healthz` answers any name.
3. **`security_headers`**: a strict CSP (`script-src 'self'`, `style-src 'self'`, no
   inline code), `frame-ancestors 'none'`, COOP and CORP same-origin, a
   `Permissions-Policy` that turns off devices, and `Server` without a version.
4. **`https_only`** (`FORCE_HTTPS`): over plain HTTP only `/healthz` and the redirect
   page are served. That page moves the browser to HTTPS from its own `location`, never
   from the `Host` header.
5. **`authentication`**: without credentials only the setup works (with the one-time
   code from the log); then a session cookie or HTTP Basic. `LoginLimiter` refuses an
   address after 10 failures in 15 minutes (`LOGIN_MAX_FAILURES`, `LOGIN_WINDOW`) and
   allows one check at a time per address. An IPv6 /64 that sent as many is only slowed
   down (every address of it waits 2 s, doubled for every 10 more failures, at most
   30 s): a LAN often shares one /64, and a host that takes a new address for each try
   still cannot guess at full speed. Past 100 failures overall it slows down only the
   clients that failed. Nobody is ever locked out by others.
6. **`csrf`**: every request that is not a GET needs `X-Requested-With: fetch` (a
   header other sites cannot send without a preflight) and a matching `Origin`.

Then the gateway answers its own endpoints (`/api/login`, `/api/logout`, `/api/setup`,
`/api/auth`) and pages, or forwards `/api/…` and `/metrics` to the core with
`forward`. It **builds the core's headers from scratch**: only `Content-Type`,
`Accept`, and its own `X-Webui-User` and `X-Webui-Client`. A client therefore cannot
forge who it is.

In the core, `core_guard` refuses a peer whose user id (`SO_PEERCRED`) is not the
gateway's or root's, and writes every non-GET request to the audit log with the user
and client the gateway gave, with its status: a 500 from an unexpected error too.
A body that is no valid JSON (also one nested too deep, or an integer past Python's
limit of digits) is a 400 `invalid_json` in both processes (`httputil.read_json`),
never a 500 whose traceback would fill the log before any login. `settings.from_dict` is the only gate for values: drive
paths, `pwmN` files under `/sys/class/hwmon`, hwmon inputs and chips under `/sys`, no
`..`, everything checked before it reaches a command line or a file.

## Credentials and sessions

`auth.py` (in the gateway) stores a scrypt hash in `/config/web/auth.json` (mode 0600),
or uses `WEBUI_USERNAME`/`WEBUI_PASSWORD`, which win and cannot be changed from the
page. Sessions live in memory: 7 days, or 12 hours without a request, at most 1000.
Changing the password needs the current one and ends every session: on the event loop
once the new hash is written (`end_all_sessions`), never in the thread that computes
it, where a request of a stolen session running at the same moment could put its
session back. Credentials can
never be removed: an installation must not go back to "open for setup".

## TLS

`tls.py`. With `ENABLE_TLS` (the default) the gateway uses `TLS_CERT`/`TLS_KEY`, or a
self-signed pair it makes once with `openssl` in `/config/web/tls`. The core copies a
given pair to `/config/web-tls` only from paths the gateway cannot change, and only PEM
files, so a gateway taken over cannot make root read an arbitrary file.

`DualListener` serves HTTP and HTTPS on the same port: it peeks at the first byte of
each connection, and `0x16` (a TLS handshake) goes to TLS. There is no HSTS on purpose:
it would lock users out of a self-signed certificate. A certificate that fails leaves
HTTPS off with an error in the log; the fans come first.

## Pages

The page never puts server data into HTML: it builds the DOM with `el()` and
`textContent`, never `innerHTML`, since drive models and log lines are not trusted.
Charts and bars are SVG attributes, never style attributes, which the CSP would block.
There are no external resources: no CDN, no fonts, nothing the browser fetches from
elsewhere.

## Checks in CI

Bandit, ruff with the `S` rules, CodeQL, hadolint and shellcheck run on every push. The
tests cover the gateway in isolation (it imports nothing that needs root) and the image
(the gateway runs as its user).

## Release

The bare metal tarball holds a wheel that `install.sh` runs as root, so the release
workflow (`release.yml`) builds it only from tools pinned with their hashes:
`packaging/build-requirements.txt` (`build`, `packaging`, `pyproject-hooks`,
`setuptools`), installed with `--require-hashes --only-binary :all:`, then
`python -m build --no-isolation`, which fetches nothing more. The checkout keeps no
token (`persist-credentials: false`), and the write token (`GH_TOKEN`) is given to the
upload step alone. `tests/test_packaging.py` checks all three.
