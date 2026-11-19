# Documentation

How hddfancontrol web UI works inside, and why it is built the way it is. For
installing and using it, start from the [README](../README.md).

| Page | What it explains |
| --- | --- |
| [architecture.md](architecture.md) | The processes (core, gateway, daemons), the modules, where the data lives, start and stop |
| [fan-control.md](fan-control.md) | How temperatures are measured, the PWM files, the curve, auto and manual modes, the safety rules, the PWM test, spin down |
| [monitoring.md](monitoring.md) | History, SMART and self-tests, alerts, notifications, schedules, the hardware support request |
| [security.md](security.md) | The trust boundary, a request step by step, credentials, TLS, the page |
| [web-ui.md](web-ui.md) | The page: files, rules, languages, theme, tabs |
| [decisions.md](decisions.md) | Every choice that shapes the program, with its reason |
| [bmc.md](bmc.md) | EXPERIMENTAL: server fans through a Dell iDRAC or HPE iLO |

Elsewhere: [CONTRIBUTING.md](../CONTRIBUTING.md) (setting up, checks, trying the web UI
without touching the fans), [SECURITY.md](../SECURITY.md) (reporting a vulnerability,
protections in short), [CHANGELOG.md](../CHANGELOG.md).

## Keeping these pages true

These pages are part of the code. A change that alters how something works, a safety
rule, a default or a design choice updates the page that describes it in the same pull
request, and a new choice gets an entry in [decisions.md](decisions.md). Numbers quoted
here name the constant they come from (`CURVE_COOLDOWN`, `STALE_S`…), so they can be
checked against the code.
