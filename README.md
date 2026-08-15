# IkeaEnergy

Battery and status reporting for an IKEA **TRÅDFRI** gateway.

`ikea_energy.py` reads every device paired to the gateway over CoAP/DTLS and
emits either:

- a **JSON snapshot** on stdout, for piping into monitoring, or
- an **HTML report with charts**, written to disk or e-mailed via Gmail SMTP or
  AWS SES.

The report leads with a KPI row (device count, how many need attention, lowest
battery), then a per-device battery chart, a "time since last report" chart, and
a full device table.

---

## Contents

- [Which gateway is this for?](#which-gateway-is-this-for)
- [Read this before you install](#read-this-before-you-install)
- [Install](#install)
  - [Docker](#docker-recommended-for-production)
  - [Raspberry Pi 4](#raspberry-pi-4)
  - [Linux / macOS](#linux--macos)
- [Creating the production `.env`](#creating-the-production-env)
  - [1. Gateway settings](#1-gateway-settings)
  - [2. Pair once to get a PSK](#2-pair-once-to-get-a-psk)
  - [3. Thresholds](#3-thresholds)
  - [4a. E-mail via Gmail SMTP](#4a-e-mail-via-gmail-smtp)
  - [4b. E-mail via AWS SES](#4b-e-mail-via-aws-ses)
  - [5. Verify](#5-verify)
- [Usage](#usage)
- [Scheduling](#scheduling)
- [Chart design notes](#chart-design-notes)
- [Troubleshooting](#troubleshooting)
- [Security](#security)

---

## Which gateway is this for?

The **TRÅDFRI gateway** — the small round white puck with a 16-character
security code on a sticker underneath.

It is *not* for the newer **DIRIGERA** hub. The two are unrelated at the
protocol level, so the popular `dirigera` Python package cannot talk to a
TRÅDFRI gateway:

|              | TRÅDFRI gateway            | DIRIGERA hub              |
| ------------ | -------------------------- | ------------------------- |
| Transport    | CoAP over DTLS, UDP `5684` | HTTPS REST, TCP `8443`    |
| Auth         | Pre-shared key from the sticker code | OAuth bearer token, hub action button |
| Python lib   | `pytradfri`                | `dirigera`                |

This project uses `pytradfri`.

---

## Read this before you install

**The gateway transport needs a build toolchain.**

TRÅDFRI speaks DTLS-PSK, which `pytradfri` gets from `DTLSSocket`. That package
publishes **no wheels at all** — not for any platform, not for any Python
version, in its entire release history. Installing it compiles a Cython
extension around tinydtls, which requires `autoconf`, `automake`, `libtool` and
a C compiler.

So pick one:

| Where you run it | Live gateway reads | How |
| --- | --- | --- |
| **Docker** | ✅ | Recommended for production. Toolchain confined to the build stage. |
| **Linux / macOS** | ✅ | `apt-get install build-essential autoconf automake libtool` (Linux) or `brew install autoconf automake libtool` (macOS) |

`--demo` feeds the pipeline synthetic devices that exercise every status path,
so you can build and test the whole report without a gateway.

---

## Install

### Docker (recommended for production)

```bash
cp .env.example .env      # then edit it — see the next section
docker compose build
docker compose run --rm ikea-energy --json
```

### Raspberry Pi 4

A Pi 4 is a good home for this — it is on the same LAN as the gateway and always
on. One hard requirement:

> **Use 64-bit Raspberry Pi OS.** Confirm with `uname -m`, which must print
> `aarch64`. On 32-bit `armv7l` this is painful to the point of impractical.

The reason is wheel availability, not the Pi's speed:

| Dependency | `aarch64` (64-bit) | `armv7l` (32-bit) |
| --- | --- | --- |
| matplotlib, numpy, pillow, contourpy, kiwisolver | prebuilt wheels | **none published** |
| pydantic-core | prebuilt wheel | prebuilt wheel |
| DTLSSocket | compiles in the builder stage | compiles in the builder stage |

On `aarch64` the only thing that compiles is `DTLSSocket`, which takes a couple
of minutes; everything else is a wheel download. On `armv7l` you would also
build numpy, matplotlib and pillow from source, which needs freetype, libjpeg
and zlib headers and can exhaust RAM on a 2 GB Pi.

```bash
uname -m                       # expect: aarch64

git clone <your-repo> /opt/ikeaenergy && cd /opt/ikeaenergy
cp .env.example .env
nano .env                      # see "Creating the production .env" below

docker compose build           # a few minutes; DTLSSocket is the only compile
docker compose run --rm ikea-energy --demo --json    # smoke test
docker compose run --rm ikea-energy --pair           # then paste the PSK into .env
docker compose run --rm ikea-energy --email
```

**Set `TZ` in `.env`** (e.g. `TZ=Europe/Stockholm`). Containers run on UTC, and
the report header and subject line are rendered in local time — without it an
08:00 report arrives stamped 06:00.

Cross-building from a faster machine is possible if the Pi build feels slow:

```bash
docker buildx build --platform linux/arm64 -t ikea-energy:latest --load .
```

`python:3.13-slim` is multi-arch, so the same `Dockerfile` covers `amd64` and
`arm64` with no changes.

### Linux / macOS

```bash
# Debian/Ubuntu
sudo apt-get update
sudo apt-get install -y build-essential autoconf automake libtool

# macOS
xcode-select --install
brew install autoconf automake libtool

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

---

## Creating the production `.env`

Configuration lives in a `.env` file next to `ikea_energy.py`. **`.env` is
gitignored and must never be committed** — it holds the gateway PSK and your
SMTP or AWS credentials. The committed template is `.env.example`, which
contains only defaults and placeholders.

The relevant `.gitignore` rules:

```gitignore
.env
.env.*
!.env.example
```

That ignores `.env` and any variant such as `.env.production`, while keeping the
template tracked. Verify at any time with:

```bash
git check-ignore -v .env      # should print the matching rule
git status --short             # .env must NOT appear
```

Start from the template:

```bash
cp .env.example .env
```

Then work through the sections below. Every variable is documented inline in
`.env.example` as well.

### 1. Gateway settings

Find the gateway's IP in your router's DHCP client list — the hostname looks
like `GW-XXXXXXXXXXXX` — or with `arp -a`.

```dotenv
TRADFRI_HOST=192.168.1.50
TRADFRI_IDENTITY=ikea_energy
TRADFRI_PSK=
TRADFRI_SECURITY_CODE=
```

> **Give the gateway a DHCP reservation** in your router. If its address moves,
> every scheduled run fails until you edit `TRADFRI_HOST`.

### 2. Pair once to get a PSK

The 16-character **security code** on the sticker underneath the gateway is
traded once for a durable **pre-shared key**. Put the code in
`TRADFRI_SECURITY_CODE`, leave `TRADFRI_PSK` empty, and run:

```bash
python ikea_energy.py --pair
# Docker: docker compose run --rm ikea-energy --pair
```

It prints the two lines to paste into `.env`:

```dotenv
TRADFRI_IDENTITY=ikea_energy
TRADFRI_PSK=aBcD1234EfGh5678
```

Then **blank out `TRADFRI_SECURITY_CODE`** — it is no longer needed, and the PSK
alone is what grants access.

> **The gateway mints exactly one PSK per identity and will not re-issue it.**
> If you lose the PSK, you cannot ask for the same one again. To recover, pick a
> *new* `TRADFRI_IDENTITY` (e.g. `ikea_energy2`), clear `TRADFRI_PSK`, and pair
> again. The script refuses to run `--pair` while `TRADFRI_PSK` is set, so it
> cannot clobber a working key by accident.

### 3. Thresholds

```dotenv
BATTERY_CRITICAL_PCT=20     # at or below this -> critical (red)
BATTERY_WARNING_PCT=40      # at or below this -> low (amber)
STALE_HOURS=24              # no report for this long -> stale; 3x -> critical
```

`BATTERY_WARNING_PCT` must be greater than `BATTERY_CRITICAL_PCT`; the script
refuses to start otherwise.

### 4a. E-mail via Gmail SMTP

Gmail needs an **App Password**, not your account password. Requirements:

1. 2-Step Verification must be enabled on the Google account.
2. Create the password at <https://myaccount.google.com/apppasswords>.
3. Paste the 16 characters into `SMTP_PASSWORD`. Spaces are stripped
   automatically, so the `abcd efgh ijkl mnop` form Google displays works
   verbatim.

```dotenv
EMAIL_BACKEND=smtp
EMAIL_FROM=ikea-reports@gmail.com
EMAIL_FROM_NAME=IKEA Energy
EMAIL_TO=you@example.com,someone@example.com
EMAIL_SUBJECT_PREFIX=[IKEA Energy]

SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_STARTTLS=true
SMTP_USERNAME=ikea-reports@gmail.com
SMTP_PASSWORD=abcdefghijklmnop
```

Port and TLS mode must agree: **587 with `SMTP_STARTTLS=true`**, or **465 with
`SMTP_STARTTLS=false`** (implicit TLS). `EMAIL_FROM` must be the account in
`SMTP_USERNAME` or an alias it is permitted to send as.

> Google Workspace accounts with SSO often have app passwords disabled by
> policy. Use AWS SES or a Workspace SMTP relay instead.

### 4b. E-mail via AWS SES

```dotenv
EMAIL_BACKEND=ses
EMAIL_FROM=ikea-reports@yourdomain.com
EMAIL_TO=you@example.com
AWS_REGION=us-east-1
SES_CONFIGURATION_SET=
```

Prerequisites:

1. **Verify the sender** — the address or its whole domain — in the SES console,
   *in the region you set*. SES is region-scoped: an identity verified in
   `us-east-1` cannot send from `eu-west-1`.
2. **Leave the SES sandbox**, or verify each recipient too. In the sandbox you
   may only send to verified addresses.
3. Grant the `ses:SendEmail` permission.

For credentials, **prefer the ambient chain** — leave `AWS_ACCESS_KEY_ID` and
`AWS_SECRET_ACCESS_KEY` empty and let an EC2/ECS instance role, `aws configure`
profile, or `AWS_PROFILE` supply them. No long-lived keys on disk:

```dotenv
AWS_ACCESS_KEY_ID=
AWS_SECRET_ACCESS_KEY=
AWS_PROFILE=ikea-reports
```

Minimal IAM policy:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": "ses:SendEmail",
      "Resource": "*"
    }
  ]
}
```

### 5. Verify

Work outward from the cheapest check, so a failure tells you exactly which layer
broke:

```bash
# 1. Config parses, report pipeline works, no gateway or network involved.
python ikea_energy.py --demo --html-out out/report.html

# 2. E-mail credentials and formatting, still no gateway.
python ikea_energy.py --demo --email

# 3. The real gateway. Linux/WSL/Docker only.
python ikea_energy.py --json

# 4. The whole thing.
python ikea_energy.py --email
```

---

## Usage

```
python ikea_energy.py [output] [source] [misc]

output:
  --json                 JSON snapshot to stdout (default if nothing else given)
  --email                send the HTML chart report
  --html-out PATH        write the HTML report and chart PNGs to PATH

source:
  --demo                 use built-in synthetic devices; contacts no gateway
  --pair                 trade TRADFRI_SECURITY_CODE for a PSK, print it, exit
  --env-file PATH        read configuration from PATH instead of ./.env

misc:
  --fail-on-alert        exit 2 when any device is critical
  -v, --verbose          debug logging
  -q, --quiet            warnings and errors only
```

`--json` and `--email` compose, so one gateway read can feed both a log and an
inbox:

```bash
python ikea_energy.py --email --json > snapshots/$(date +%F).json
```

**Exit codes** — `0` success · `1` configuration or gateway error ·
`2` `--fail-on-alert` and at least one device is critical · `130` interrupted.

All logging goes to **stderr**, so stdout is always clean JSON:

```bash
python ikea_energy.py --json --quiet | jq '.devices[] | select(.battery_status=="critical") | .name'
```

---

## Scheduling

### cron (Linux / the Docker host)

```cron
# 08:00 daily. Absolute paths — cron's PATH and cwd are not yours.
0 8 * * * cd /opt/ikeaenergy && /opt/ikeaenergy/.venv/bin/python ikea_energy.py --email --quiet >> /var/log/ikea_energy.log 2>&1
```

With Docker:

```cron
0 8 * * * cd /opt/ikeaenergy && /usr/bin/docker compose run --rm ikea-energy --email --quiet >> /var/log/ikea_energy.log 2>&1
```

---

## Chart design notes

Two things here are deliberate and worth knowing before you "fix" them.

**Healthy batteries are grey, not green.** The obvious green/amber/red ramp
fails colour-vision-deficiency validation: status-good `#0ca30c` against
status-critical `#d03b3b` measures an OKLab ΔE of **4.1 under deuteranopia**,
meaning a red-green colourblind reader cannot tell a full battery from a dead
one. Dropping green and letting healthy bars recede to the muted grey token
leaves amber vs red at **ΔE 24.4 (deuteranopia) / 28.4 (normal vision)** in both
light and dark mode. This is the "emphasis" form: only the exceptions carry
colour.

**Colour is never the only channel.** Every non-OK bar is labelled in words
(`18% critical`, `2.5d stale`), the table repeats every charted value with a
glyph *and* a word per status, and both charts ship alt text pointing at the
table. Amber sits below 3:1 contrast on the light surface by design, which is
exactly why those labels are mandatory rather than decorative.

Charts render in both light and dark variants and are wired into the e-mail with
`<picture>` + `prefers-color-scheme`, so clients that support dark mode get the
dark surface and everything else falls back to light.

The "time since last report" chart is an **exception chart**: devices that
reported within the last hour are counted in a note instead of drawn, because a
dozen zero-length bars carry no information. If nothing is stale the chart is
omitted entirely and the table carries the detail.

---

## Troubleshooting

**`No CoAP/DTLS transport available`**
`pip install -r requirements.txt` failed to build `DTLSSocket`. See
[Read this before you install](#read-this-before-you-install). Use Docker, or
`--demo`.

**`Building wheel for DTLSSocket ... error` / `sh: autoconf: command not found`**
The build toolchain is missing. On Debian/Ubuntu:
`sudo apt-get install -y build-essential autoconf automake libtool`.

**`Could not read gateway ...`**
In order of likelihood: `TRADFRI_HOST` is stale because DHCP moved the gateway;
`TRADFRI_PSK` does not match `TRADFRI_IDENTITY`; UDP 5684 is blocked or the
gateway is on another VLAN; the gateway is mid-firmware-update. Confirm
reachability with `ping` first, and note that the gateway answers only CoAP —
it has no web UI to load.

**`TRADFRI_PSK is already set`** on `--pair`
Intentional guard: pairing again would discard a working key. To rotate, choose
a new `TRADFRI_IDENTITY`, clear `TRADFRI_PSK`, and re-run.

**`SMTP rejected the credentials`**
Gmail requires an App Password with 2-Step Verification enabled. Workspace SSO
accounts may have them disabled by policy — use SES.

**SMTP times out or hangs**
`SMTP_PORT` and `SMTP_STARTTLS` disagree. 587 needs `true`, 465 needs `false`.

**`SES rejected the message (MessageRejected)`**
The sender is not verified *in `AWS_REGION`*, or the account is still in the
sandbox and the recipient is unverified.

**Charts look wrong in the e-mail**
Some clients strip `<style>` blocks and `<picture>`. The layout is
table-based with inline styles so it degrades to the light theme rather than
breaking. Compare against `--html-out`, which is the same HTML in a browser.

**Battery shows `—` for a device**
It is mains-powered. Bulbs, outlets and signal repeaters report no battery, so
they are excluded from the battery chart and marked `mains` in the table.

---

## Security

- `.env` holds the gateway PSK and mail credentials, is gitignored, and should
  be `chmod 600` on any shared host.
- The PSK grants full control of every device on the gateway, not just read
  access. Treat it like a password.
- Prefer IAM roles over static AWS keys; leave `AWS_ACCESS_KEY_ID` and
  `AWS_SECRET_ACCESS_KEY` empty to use the ambient credential chain.
- The container runs as a non-root user (uid 10001) with a read-only root
  filesystem, all capabilities dropped, and `no-new-privileges`.
- Reports name your devices and their battery levels. That is mild, but it is
  household telemetry — think about who is on `EMAIL_TO`.
