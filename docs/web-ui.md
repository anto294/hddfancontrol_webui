# The web page

How the page in `hddfancontrol_webui/static/` is built and the rules it keeps.

## No build step

The page is plain HTML, CSS and classic scripts, served as they are: no bundler, no
framework, no package to install, no external resource. A user can read what runs in
their browser, and the image needs nothing beyond Python.

| File | Job |
| --- | --- |
| `index.html` | Every tab as a `<section>`; texts are `data-i18n` keys |
| `i18n.js` | Languages, `t()`, the language picker and the theme switch |
| `icons.js` | Line icons drawn for the project, built as SVG nodes (`icon(name)`, `data-icon`) |
| `app.js` | State, helpers (`el`, `ltr`, `fmtTemp`…), banners, the overview, polling, the start |
| `fans.js`, `drives.js`, `history.js`, `settings.js`, `security.js` | One tab each, sharing the globals of `app.js` |
| `wizard.js` | The guided setup |
| `login.js`, `https.js` | The login and HTTPS redirect pages |

The scripts load in that order with `defer`; later ones use the globals of earlier
ones.

## Rules

- **No HTML from data.** The DOM is built with `el()` and `textContent`. Never
  `innerHTML` with anything that came from the server.
- **No inline style or script.** The CSP forbids them. Charts and bars are SVG with
  attributes; a dynamic look is a class change.
- **Animations by class only**, and updated in place, so an animation does not restart
  at every poll; `prefers-reduced-motion` turns them off.
- **No polling while the tab is hidden** (`every()`), except the status during manual
  control: the server ends manual control when no page asks any more.
- **Rates in the page.** The hardware snapshot carries raw counters (I/O sectors, CPU
  ticks) and a monotonic `clock`; `updateRates` turns two snapshots into rates, so the
  server keeps no state per client.
- **Errors are codes.** The API answers `{error: code, problems: [{field, code,
  params}]}`; the page translates `error.<code>` and puts each problem under its field.
- **A network error is only a network error.** `pollStatus` shows "the server does not
  answer" only when the request fails; each part of the page is then drawn on its own,
  and a bug in one goes to the console without hiding the others (the banners of the
  safety rules first).

## Languages

Texts are flat keys in `static/i18n/<lang>.json`: English, Italian, Spanish, French,
Chinese, Hindi and Arabic. A new key goes into every file, translated by meaning, not
word for word. `tests/test_translations.py` fails when a key, a placeholder, a server
error code or a status lacks a text, or when a language is missing from `LANGUAGES`
(`i18n.js`), `NOTIFY_LANGUAGES` (`settings.py`) or the notification language options
of `index.html`. Alerts sent by the server use the same files (`alert.<kind>`).

Arabic turns the page right to left (`dir="rtl"`). So the CSS uses logical properties
(`margin-inline-start`, `text-align: start`), never left or right, and a number with
its unit goes through `ltr()`, which wraps it in direction isolates so "48.3 °C" does
not become "°C 48.3".

## Theme

Light, dark or the system's: `data-theme` on `<html>`, remembered in `localStorage`.
`style.css` defines colors as variables, with the dark ones for the system setting and
for `data-theme="dark"`.

## Tabs

| Tab | What |
| --- | --- |
| Overview | The Control card in three groups (fan control: state, mode, the speeds given; services: the programs and **Restart**; host: CPU), drives with bars and trends, fans, the bay map with animated drives (spinning platters, a head and a LED driven by real I/O; moving electrons for SSDs) |
| Fans | Every PWM output, its speed, RPM and name; manual control; the hardware support request |
| Drives | SMART per drive, self-tests |
| History | Charts of temperatures, PWM and RPM, CSV export |
| Settings | Everything of `settings.json`, folded sections, a cron composer for each schedule, backup |

The notification address comes masked (`•••` for its token, password or ntfy topic):
the field shows it so, with a note, and sends it back unchanged to keep it; a click
selects it all, so typing replaces it. A backup holds it masked too, and importing one
keeps the address in force.
| PWM test | Start and stop values of the fans |
| Security | Credentials |
| Logs | The last lines of every source |

The **guided setup** opens by itself while the settings come from the environment with
no drive and no fan, until it is closed once (`localStorage` `setupSkipped`). It builds
a settings document from the answers and saves it through `PUT /api/settings`, like the
Settings tab.

Names (`labels.json`) and the bay map (`bays.json`) are saved on their own endpoints and
never restart fan control.

In the bay map editor (Drives tab) the bays move like blocks: drag one by its grip onto
another and the two swap their drives, or press one grip and then the other (the way for a
keyboard). It uses pointer events, not HTML drag and drop, which touch screens lack. Nothing
is saved until **Save the map**.
