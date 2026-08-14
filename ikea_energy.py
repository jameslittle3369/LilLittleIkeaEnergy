#!/usr/bin/env python3
"""Battery and status reporter for an IKEA TRADFRI gateway.

Reads every device paired to the gateway over CoAP/DTLS, classifies battery
level and reachability, and emits either a JSON snapshot on stdout or an HTML
e-mail report with charts (Gmail SMTP or AWS SES).

    python ikea_energy.py --pair                    # one-time: mint a PSK
    python ikea_energy.py --json                    # snapshot to stdout
    python ikea_energy.py --html-out out/report.html
    python ikea_energy.py --email
    python ikea_energy.py --demo --html-out out/report.html   # no gateway needed

Configuration comes from .env (see .env.example). Every log line goes to
stderr, so --json output on stdout is always clean JSON.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import os
import smtplib
import sys
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from pathlib import Path
from typing import Any, Sequence

LOG = logging.getLogger("ikea_energy")

# --------------------------------------------------------------------------- #
# Status vocabulary
# --------------------------------------------------------------------------- #

OK = "ok"
WARNING = "warning"
CRITICAL = "critical"
UNKNOWN = "unknown"  # mains-powered: no battery to report

#: Rank used for sorting and for rolling many device statuses into one verdict.
SEVERITY = {OK: 0, UNKNOWN: 0, WARNING: 1, CRITICAL: 2}

#: Short glyph + word, so status never rides on colour alone.
STATUS_LABEL = {
    OK: ("●", "ok"),
    WARNING: ("▲", "low"),
    CRITICAL: ("■", "critical"),
    UNKNOWN: ("—", "mains"),
}


# --------------------------------------------------------------------------- #
# Palette — values from the dataviz reference palette.
#
# Battery bars use the "emphasis" form: healthy devices recede to the muted ink
# token and only warning/critical carry a status hue. That is deliberate. The
# obvious green/amber/red ramp fails CVD validation — good #0ca30c against
# critical #d03b3b measures OKLab dE 4.1 under deuteranopia, i.e. the two are
# indistinguishable to a red-green colourblind reader. Dropping green leaves
# warning vs critical at dE 24.4 (deutan) / 28.4 (normal vision) in both modes.
# Warning #fab219 sits below 3:1 on the light surface by design, so every bar
# also carries a text label and the report ships a full table view.
# --------------------------------------------------------------------------- #

STATUS_COLOR = {CRITICAL: "#d03b3b", WARNING: "#fab219", "good": "#0ca30c"}

THEMES: dict[str, dict[str, str]] = {
    "light": {
        "surface": "#fcfcfb",
        "page": "#f9f9f7",
        "ink": "#0b0b0b",
        "ink_secondary": "#52514e",
        "muted": "#898781",
        "grid": "#e1e0d9",
        "axis": "#c3c2b7",
        "border": "rgba(11,11,11,0.10)",
        "tile": "#ffffff",
    },
    "dark": {
        "surface": "#1a1a19",
        "page": "#0d0d0d",
        "ink": "#ffffff",
        "ink_secondary": "#c3c2b7",
        "muted": "#898781",
        "grid": "#2c2c2a",
        "axis": "#383835",
        "border": "rgba(255,255,255,0.10)",
        "tile": "#222221",
    },
}

#: Healthy bars use the muted ink token: visible, clearly recessive, and
#: identical in both modes (the token is mode-invariant in the reference palette).
DEEMPHASIS = "#898781"

FONT_STACK = ["Segoe UI", "Helvetica Neue", "Arial", "DejaVu Sans", "sans-serif"]
DPI = 150
#: Charts are rendered wide and displayed at 600px, so they stay crisp on
#: high-DPI screens while fitting the conventional e-mail column.
EMAIL_IMAGE_WIDTH = 600


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from None


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name).lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{name} must be a boolean, got {raw!r}")


def _env_list(name: str) -> list[str]:
    return [part.strip() for part in _env(name).split(",") if part.strip()]


class ConfigError(RuntimeError):
    """Raised when .env is missing or contradictory."""


class GatewayError(RuntimeError):
    """Raised when the gateway cannot be reached or refuses the credentials."""


@dataclasses.dataclass(frozen=True)
class Settings:
    host: str
    identity: str
    psk: str
    security_code: str
    transport: str
    timeout: int

    battery_critical: int
    battery_warning: int
    stale_hours: int

    email_backend: str
    email_from: str
    email_from_name: str
    email_to: list[str]
    email_cc: list[str]
    email_reply_to: list[str]
    subject_prefix: str

    smtp_host: str
    smtp_port: int
    smtp_starttls: bool
    smtp_username: str
    smtp_password: str
    smtp_timeout: int

    aws_region: str
    aws_access_key_id: str
    aws_secret_access_key: str
    aws_profile: str
    ses_configuration_set: str

    output_dir: Path
    site_name: str

    @classmethod
    def load(cls, env_file: str | None = None) -> Settings:
        try:
            from dotenv import load_dotenv
        except ImportError:  # pragma: no cover - dependency is declared
            raise ConfigError(
                "python-dotenv is not installed. Run:\n"
                "    pip install -r requirements.txt"
            ) from None

        path = Path(env_file) if env_file else Path(".env")
        if path.is_file():
            load_dotenv(path, override=False)
            LOG.debug("loaded configuration from %s", path)
        elif env_file:
            raise ConfigError(f"--env-file {path} does not exist")
        else:
            LOG.warning(
                "no .env found; relying on the ambient environment "
                "(copy .env.example to .env to fix)"
            )

        critical = _env_int("BATTERY_CRITICAL_PCT", 20)
        warning = _env_int("BATTERY_WARNING_PCT", 40)
        if warning <= critical:
            raise ConfigError(
                f"BATTERY_WARNING_PCT ({warning}) must be greater than "
                f"BATTERY_CRITICAL_PCT ({critical})"
            )

        transport = _env("TRADFRI_TRANSPORT", "auto").lower()
        if transport not in {"auto", "aiocoap", "libcoap"}:
            raise ConfigError(
                f"TRADFRI_TRANSPORT must be auto, aiocoap or libcoap, got {transport!r}"
            )

        backend = _env("EMAIL_BACKEND", "smtp").lower()
        if backend not in {"smtp", "ses", "none"}:
            raise ConfigError(
                f"EMAIL_BACKEND must be smtp, ses or none, got {backend!r}"
            )

        return cls(
            host=_env("TRADFRI_HOST"),
            identity=_env("TRADFRI_IDENTITY", "ikea_energy"),
            psk=_env("TRADFRI_PSK"),
            security_code=_env("TRADFRI_SECURITY_CODE"),
            transport=transport,
            timeout=_env_int("TRADFRI_TIMEOUT", 10),
            battery_critical=critical,
            battery_warning=warning,
            stale_hours=_env_int("STALE_HOURS", 24),
            email_backend=backend,
            email_from=_env("EMAIL_FROM"),
            email_from_name=_env("EMAIL_FROM_NAME"),
            email_to=_env_list("EMAIL_TO"),
            email_cc=_env_list("EMAIL_CC"),
            email_reply_to=_env_list("EMAIL_REPLY_TO"),
            subject_prefix=_env("EMAIL_SUBJECT_PREFIX", "[IKEA Energy]"),
            smtp_host=_env("SMTP_HOST", "smtp.gmail.com"),
            smtp_port=_env_int("SMTP_PORT", 587),
            smtp_starttls=_env_bool("SMTP_STARTTLS", True),
            smtp_username=_env("SMTP_USERNAME"),
            # Gmail shows app passwords in groups of four; strip the spaces so a
            # pasted value works verbatim.
            smtp_password=_env("SMTP_PASSWORD").replace(" ", ""),
            smtp_timeout=_env_int("SMTP_TIMEOUT", 30),
            aws_region=_env("AWS_REGION"),
            aws_access_key_id=_env("AWS_ACCESS_KEY_ID"),
            aws_secret_access_key=_env("AWS_SECRET_ACCESS_KEY"),
            aws_profile=_env("AWS_PROFILE"),
            ses_configuration_set=_env("SES_CONFIGURATION_SET"),
            output_dir=Path(_env("REPORT_OUTPUT_DIR", "out")),
            site_name=_env("REPORT_SITE_NAME", "Home"),
        )


# --------------------------------------------------------------------------- #
# Normalised device snapshot
# --------------------------------------------------------------------------- #


@dataclasses.dataclass
class DeviceSnapshot:
    id: str
    name: str
    model: str
    manufacturer: str
    firmware: str | None
    kind: str
    power_source: str | None
    reachable: bool
    last_seen: str | None
    last_seen_hours: float | None
    battery_percent: int | None
    is_on: bool | None
    dimmer: int | None
    cover_position: int | None
    battery_status: str = UNKNOWN
    freshness_status: str = OK
    alerts: list[str] = dataclasses.field(default_factory=list)

    @property
    def worst_status(self) -> str:
        return max(
            (self.battery_status, self.freshness_status),
            key=lambda s: SEVERITY.get(s, 0),
        )

    @property
    def has_battery(self) -> bool:
        return self.battery_percent is not None

    def to_dict(self) -> dict[str, Any]:
        data = dataclasses.asdict(self)
        data["worst_status"] = self.worst_status
        return data


def classify(devices: list[DeviceSnapshot], cfg: Settings) -> None:
    """Fill in battery_status, freshness_status and alerts, in place."""
    for dev in devices:
        if dev.battery_percent is None:
            dev.battery_status = UNKNOWN
        elif dev.battery_percent <= cfg.battery_critical:
            dev.battery_status = CRITICAL
            dev.alerts.append(
                f"battery {dev.battery_percent}% at or below "
                f"critical threshold {cfg.battery_critical}%"
            )
        elif dev.battery_percent <= cfg.battery_warning:
            dev.battery_status = WARNING
            dev.alerts.append(
                f"battery {dev.battery_percent}% at or below "
                f"warning threshold {cfg.battery_warning}%"
            )
        else:
            dev.battery_status = OK

        hours = dev.last_seen_hours
        if not dev.reachable:
            dev.freshness_status = CRITICAL
            dev.alerts.append("gateway reports the device as unreachable")
        elif hours is None:
            dev.freshness_status = OK
        elif hours > cfg.stale_hours * 3:
            dev.freshness_status = CRITICAL
            dev.alerts.append(f"last seen {hours:.0f}h ago")
        elif hours > cfg.stale_hours:
            dev.freshness_status = WARNING
            dev.alerts.append(f"last seen {hours:.0f}h ago")
        else:
            dev.freshness_status = OK


def summarise(devices: list[DeviceSnapshot], cfg: Settings) -> dict[str, Any]:
    battery_devices = [d for d in devices if d.has_battery]
    levels = [d.battery_percent for d in battery_devices]
    lowest = min(battery_devices, key=lambda d: d.battery_percent or 0, default=None)
    return {
        "devices_total": len(devices),
        "devices_with_battery": len(battery_devices),
        "unreachable": sum(1 for d in devices if not d.reachable),
        "battery_critical": sum(1 for d in devices if d.battery_status == CRITICAL),
        "battery_warning": sum(1 for d in devices if d.battery_status == WARNING),
        "stale": sum(
            1 for d in devices if d.reachable and d.freshness_status != OK
        ),
        "lowest_battery_percent": min(levels) if levels else None,
        "lowest_battery_device": lowest.name if lowest else None,
        "mean_battery_percent": round(sum(levels) / len(levels), 1) if levels else None,
        "worst_status": max(
            (d.worst_status for d in devices),
            key=lambda s: SEVERITY.get(s, 0),
            default=OK,
        ),
        "thresholds": {
            "battery_critical_pct": cfg.battery_critical,
            "battery_warning_pct": cfg.battery_warning,
            "stale_hours": cfg.stale_hours,
        },
    }


# --------------------------------------------------------------------------- #
# Gateway I/O
# --------------------------------------------------------------------------- #

_KIND_BY_ATTR = (
    ("light_control", "light"),
    ("socket_control", "socket"),
    ("blind_control", "blind"),
    ("signal_repeater_control", "signal repeater"),
    ("air_purifier_control", "air purifier"),
)


def _last_seen(raw_last_seen: int | None) -> tuple[str | None, float | None]:
    """Convert the gateway's epoch seconds to (ISO-8601 UTC, age in hours).

    Built from the raw integer rather than pytradfri's ``Device.last_seen``,
    which goes through the deprecated ``datetime.utcfromtimestamp``.
    """
    if raw_last_seen is None:
        return None, None
    seen = datetime.fromtimestamp(raw_last_seen, tz=timezone.utc)
    age = (datetime.now(timezone.utc) - seen).total_seconds() / 3600.0
    return seen.isoformat(), round(max(age, 0.0), 2)


def _to_snapshot(device: Any) -> DeviceSnapshot:
    """Normalise a pytradfri Device into a transport-agnostic snapshot."""
    raw = device.raw
    info = device.device_info

    kind = "controller"  # remotes, dimmers and sensors expose no control object
    for attr, label in _KIND_BY_ATTR:
        if getattr(raw, attr, None):
            kind = label
            break

    is_on: bool | None = None
    dimmer: int | None = None
    cover_position: int | None = None
    if raw.light_control:
        light = raw.light_control[0]
        is_on = light.state == 1
        dimmer = light.dimmer
    elif raw.socket_control:
        is_on = raw.socket_control[0].state == 1
    elif raw.blind_control:
        cover_position = raw.blind_control[0].current_cover_position

    iso, hours = _last_seen(raw.last_seen)
    return DeviceSnapshot(
        id=str(device.id),
        name=device.name or f"device {device.id}",
        model=info.model_number,
        manufacturer=info.manufacturer,
        firmware=info.firmware_version,
        kind=kind,
        power_source=info.power_source_str,
        reachable=device.reachable,
        last_seen=iso,
        last_seen_hours=hours,
        battery_percent=info.battery_level,
        is_on=is_on,
        dimmer=dimmer,
        cover_position=cover_position,
    )


def _resolve_transport(cfg: Settings) -> str:
    if cfg.transport != "auto":
        return cfg.transport
    try:
        import aiocoap  # noqa: F401
        import DTLSSocket  # noqa: F401
    except ImportError:
        import shutil

        if shutil.which("coap-client"):
            LOG.debug("transport auto -> libcoap (coap-client found on PATH)")
            return "libcoap"
        raise GatewayError(
            "No CoAP/DTLS transport available.\n"
            "  Install one:   pip install -r requirements.txt   "
            "(needs autoconf + a C compiler)\n"
            "  Or provide a coap-client binary built with DTLS support.\n"
            "  This cannot be built on stock Windows -- use WSL2 or Docker, "
            "or run with --demo.\n"
            "  See README.md 'Deployment'."
        ) from None
    LOG.debug("transport auto -> aiocoap")
    return "aiocoap"


def _require_credentials(cfg: Settings) -> None:
    if not cfg.host:
        raise ConfigError("TRADFRI_HOST is not set (see .env.example)")
    if not cfg.psk:
        raise ConfigError(
            "TRADFRI_PSK is not set. Run pairing once:\n"
            "    python ikea_energy.py --pair\n"
            "then paste the printed key into .env as TRADFRI_PSK."
        )


def read_gateway(cfg: Settings) -> tuple[list[DeviceSnapshot], dict[str, Any]]:
    """Fetch all devices plus gateway metadata."""
    _require_credentials(cfg)
    transport = _resolve_transport(cfg)
    LOG.info("reading gateway %s over %s", cfg.host, transport)
    if transport == "libcoap":
        devices, info = _read_libcoap(cfg)
    else:
        import asyncio

        devices, info = asyncio.run(_read_aiocoap(cfg))
    return [_to_snapshot(d) for d in devices], info


def _gateway_meta(info: Any) -> dict[str, Any]:
    return {
        "id": info.id,
        "firmware_version": info.firmware_version,
        "ntp_server": info.ntp_server,
        "current_time": info.current_time_iso8601,
    }


def _read_libcoap(cfg: Settings) -> tuple[list[Any], dict[str, Any]]:
    from pytradfri.api.libcoap_api import APIFactory
    from pytradfri.error import PytradfriError
    from pytradfri.gateway import Gateway

    factory = APIFactory(
        host=cfg.host, psk_id=cfg.identity, psk=cfg.psk, timeout=cfg.timeout
    )
    gateway = Gateway()
    try:
        devices = factory.request(factory.request(gateway.get_devices()))
        info = factory.request(gateway.get_gateway_info())
    except PytradfriError as exc:
        raise GatewayError(_gateway_hint(cfg, exc)) from exc
    return devices, _gateway_meta(info)


async def _read_aiocoap(cfg: Settings) -> tuple[list[Any], dict[str, Any]]:
    from pytradfri.api.aiocoap_api import APIFactory
    from pytradfri.error import PytradfriError
    from pytradfri.gateway import Gateway

    factory = await APIFactory.init(host=cfg.host, psk_id=cfg.identity, psk=cfg.psk)
    gateway = Gateway()
    try:
        devices = await factory.request(
            await factory.request(gateway.get_devices(), timeout=cfg.timeout),
            timeout=cfg.timeout,
        )
        info = await factory.request(
            gateway.get_gateway_info(), timeout=cfg.timeout
        )
    except PytradfriError as exc:
        raise GatewayError(_gateway_hint(cfg, exc)) from exc
    finally:
        await factory.shutdown()
    return devices, _gateway_meta(info)


def _gateway_hint(cfg: Settings, exc: Exception) -> str:
    return (
        f"Could not read gateway {cfg.host}: {exc}\n"
        "Common causes:\n"
        f"  - wrong TRADFRI_HOST (is {cfg.host} still the gateway's IP? DHCP moves it)\n"
        "  - TRADFRI_PSK does not match TRADFRI_IDENTITY -- re-run --pair with a "
        "NEW identity\n"
        "  - UDP 5684 blocked, or the host is on a different VLAN/subnet\n"
        "  - gateway rebooting, or busy with a firmware update"
    )


def pair(cfg: Settings) -> str:
    """Exchange the sticker security code for a durable PSK."""
    if not cfg.host:
        raise ConfigError("TRADFRI_HOST is not set (see .env.example)")
    if not cfg.security_code:
        raise ConfigError(
            "TRADFRI_SECURITY_CODE is not set. It is the 16-character code on "
            "the sticker underneath the gateway."
        )
    if cfg.psk:
        raise ConfigError(
            "TRADFRI_PSK is already set. The gateway mints one PSK per identity "
            "and refuses to re-issue it.\n"
            "To rotate, pick a new TRADFRI_IDENTITY, clear TRADFRI_PSK, and "
            "re-run --pair."
        )

    transport = _resolve_transport(cfg)
    LOG.info("pairing with %s over %s as identity %r", cfg.host, transport, cfg.identity)

    from pytradfri.error import PytradfriError

    try:
        if transport == "libcoap":
            from pytradfri.api.libcoap_api import APIFactory

            factory = APIFactory(
                host=cfg.host, psk_id=cfg.identity, timeout=cfg.timeout
            )
            return factory.generate_psk(cfg.security_code)

        import asyncio

        from pytradfri.api.aiocoap_api import APIFactory as AsyncAPIFactory

        async def _run() -> str:
            factory = await AsyncAPIFactory.init(
                host=cfg.host, psk_id=cfg.identity
            )
            try:
                return await factory.generate_psk(cfg.security_code)
            finally:
                await factory.shutdown()

        return asyncio.run(_run())
    except PytradfriError as exc:
        raise GatewayError(
            f"Pairing with {cfg.host} failed: {exc}\n"
            "Check the security code (letters are case-sensitive), and that "
            f"identity {cfg.identity!r} has not already been registered."
        ) from exc


# --------------------------------------------------------------------------- #
# Demo data — exercises every status path without a gateway
# --------------------------------------------------------------------------- #


def demo_devices() -> tuple[list[DeviceSnapshot], dict[str, Any]]:
    now = datetime.now(timezone.utc)

    def snap(
        did: str,
        name: str,
        model: str,
        kind: str,
        *,
        battery: int | None = None,
        hours: float | None = 0.1,
        reachable: bool = True,
        is_on: bool | None = None,
        dimmer: int | None = None,
        cover: int | None = None,
        firmware: str = "2.3.093",
    ) -> DeviceSnapshot:
        iso = None
        if hours is not None:
            iso = (now - timedelta(hours=hours)).isoformat()
        return DeviceSnapshot(
            id=did,
            name=name,
            model=model,
            manufacturer="IKEA of Sweden",
            firmware=firmware,
            kind=kind,
            power_source="Internal Battery" if battery is not None else "AC (Mains) power",
            reachable=reachable,
            last_seen=iso,
            last_seen_hours=hours,
            battery_percent=battery,
            is_on=is_on,
            dimmer=dimmer,
            cover_position=cover,
        )

    devices = [
        snap("65536", "Living room remote", "TRADFRI remote control", "controller", battery=9),
        snap("65537", "Hallway motion sensor", "TRADFRI motion sensor", "controller", battery=18, hours=2.5),
        snap("65538", "Bedroom dimmer", "TRADFRI wireless dimmer", "controller", battery=34),
        snap("65539", "Kitchen on/off switch", "TRADFRI on/off switch", "controller", battery=38, hours=61),
        snap("65540", "Study blind", "FYRTUR block-out roller blind", "blind", battery=62, cover=100),
        snap("65541", "Bedroom blind", "KADRILJ roller blind", "blind", battery=77, cover=0),
        snap("65542", "Garage sensor", "TRADFRI motion sensor", "controller", battery=88, hours=190, reachable=False),
        snap("65543", "Porch remote", "TRADFRI remote control", "controller", battery=95),
        snap("65544", "Living room bulb", "TRADFRI bulb E27 CWS 806lm", "light", is_on=True, dimmer=204),
        snap("65545", "Kitchen spot", "TRADFRI bulb GU10 WS 400lm", "light", is_on=False, dimmer=0),
        snap("65546", "Desk lamp", "TRADFRI bulb E14 WS 470lm", "light", is_on=True, dimmer=127),
        snap("65547", "Coffee machine plug", "TRADFRI control outlet", "socket", is_on=False),
        snap("65548", "Loft repeater", "TRADFRI signal repeater", "signal repeater", firmware="2.3.089"),
    ]
    meta = {
        "id": "GW-DEMO0000DEMO",
        "firmware_version": "1.21.36",
        "ntp_server": "pool.ntp.org",
        "current_time": now.replace(microsecond=0).isoformat(),
        "demo": True,
    }
    return devices, meta


# --------------------------------------------------------------------------- #
# Charts
# --------------------------------------------------------------------------- #


def _configure_matplotlib() -> Any:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams.update(
        {
            "font.family": "sans-serif",
            "font.sans-serif": FONT_STACK,
            "axes.unicode_minus": False,
            "svg.fonttype": "none",
        }
    )
    return plt


def _rounded_bar(ax: Any, y: float, value: float, height: float, color: str) -> None:
    """Draw a bar with a 4px-rounded data end, square at the baseline.

    The corner radius is converted separately for x and y so the ellipse reads
    as a circle on screen despite the two axes having different scales.
    """
    from matplotlib.patches import PathPatch, Rectangle
    from matplotlib.path import Path as MplPath

    y0, y1 = y - height / 2, y + height / 2
    bbox = ax.get_window_extent()
    x_lo, x_hi = ax.get_xlim()
    y_lo, y_hi = ax.get_ylim()

    radius_px = 4.0 * (DPI / 96.0)  # 4 CSS px at this raster density
    rx = radius_px * (x_hi - x_lo) / max(bbox.width, 1.0)
    ry = radius_px * (y_hi - y_lo) / max(bbox.height, 1.0)
    ry = min(ry, height / 2)

    if value <= rx * 1.5:
        # Too short to round without distorting the value; draw it square.
        ax.add_patch(
            Rectangle((0, y0), value, height, facecolor=color, edgecolor="none")
        )
        return

    verts = [
        (0, y0),
        (value - rx, y0),
        (value, y0),
        (value, y0 + ry),
        (value, y1 - ry),
        (value, y1),
        (value - rx, y1),
        (0, y1),
        (0, y0),
    ]
    codes = [
        MplPath.MOVETO,
        MplPath.LINETO,
        MplPath.CURVE3,
        MplPath.CURVE3,
        MplPath.LINETO,
        MplPath.CURVE3,
        MplPath.CURVE3,
        MplPath.LINETO,
        MplPath.CLOSEPOLY,
    ]
    ax.add_patch(
        PathPatch(MplPath(verts, codes), facecolor=color, edgecolor="none")
    )


def _new_axes(
    plt: Any,
    rows: int,
    theme: dict[str, str],
    *,
    width_in: float = 7.0,
    left_in: float = 2.05,
    right_in: float = 1.35,
    top_in: float = 0.92,
    bottom_in: float = 0.62,
    pitch_in: float = 0.30,
) -> tuple[Any, Any]:
    """A horizontal-bar canvas sized so bars stay thin and the axis band fits."""
    plot_h = max(rows, 1) * pitch_in
    height_in = top_in + plot_h + bottom_in
    fig = plt.figure(figsize=(width_in, height_in), dpi=DPI)
    fig.patch.set_facecolor(theme["surface"])
    ax = fig.add_axes(
        [
            left_in / width_in,
            bottom_in / height_in,
            (width_in - left_in - right_in) / width_in,
            plot_h / height_in,
        ]
    )
    ax.set_facecolor(theme["surface"])
    for side in ("top", "right", "bottom"):
        ax.spines[side].set_visible(False)
    ax.spines["left"].set_color(theme["axis"])
    ax.spines["left"].set_linewidth(0.8)
    ax.tick_params(
        axis="both", length=0, colors=theme["muted"], labelsize=7.5, pad=6
    )
    ax.xaxis.grid(True, color=theme["grid"], linewidth=0.8, linestyle="-")
    ax.set_axisbelow(True)
    return fig, ax


def _truncate(text: str, limit: int = 26) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _save(fig: Any, path: Path, theme: dict[str, str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=DPI, facecolor=theme["surface"])
    import matplotlib.pyplot as plt

    plt.close(fig)
    LOG.debug("wrote %s", path)
    return path


def chart_battery(
    devices: list[DeviceSnapshot], cfg: Settings, mode: str, path: Path
) -> Path | None:
    """Battery percentage per battery-powered device, lowest first."""
    plt = _configure_matplotlib()
    theme = THEMES[mode]

    rows = sorted(
        (d for d in devices if d.has_battery),
        key=lambda d: (d.battery_percent or 0, d.name),
    )
    if not rows:
        return None

    fig, ax = _new_axes(plt, len(rows), theme, right_in=1.6)
    # A little headroom past 100 so a 95%+ tip label never crowds the edge.
    ax.set_xlim(0, 104)
    ax.set_ylim(len(rows) - 0.5, -0.5)  # first row at the top
    ax.set_xticks([0, 25, 50, 75, 100])
    ax.set_xticklabels(["0", "25", "50", "75", "100%"])
    ax.set_yticks(range(len(rows)))
    ax.set_yticklabels(
        [_truncate(d.name) for d in rows], color=theme["ink_secondary"], fontsize=8
    )

    # A quiet anchor at the critical threshold. Deliberately unlabelled: the
    # legend already spells out both cut-offs, and an inline label here has
    # nowhere to sit that does not collide with a bar or the legend.
    ax.axvline(
        cfg.battery_critical, color=theme["axis"], linewidth=0.8, linestyle="-", zorder=1
    )

    fig.canvas.draw()  # window extents must be real before rounding the ends
    bar_h = 0.5
    for i, dev in enumerate(rows):
        colour = STATUS_COLOR.get(dev.battery_status, DEEMPHASIS)
        _rounded_bar(ax, i, float(dev.battery_percent or 0), bar_h, colour)
        _, word = STATUS_LABEL[dev.battery_status]
        label = f"{dev.battery_percent}%"
        if dev.battery_status in (WARNING, CRITICAL):
            label = f"{label}  {word}"
        ax.annotate(
            label,
            xy=(dev.battery_percent or 0, i),
            xytext=(6, 0),
            textcoords="offset points",
            va="center",
            color=theme["ink"],
            fontsize=7.5,
        )

    from matplotlib.patches import Patch

    ax.legend(
        handles=[
            Patch(facecolor=STATUS_COLOR[CRITICAL], label=f"critical (<={cfg.battery_critical}%)"),
            Patch(facecolor=STATUS_COLOR[WARNING], label=f"low (<={cfg.battery_warning}%)"),
            Patch(facecolor=DEEMPHASIS, label="ok"),
        ],
        loc="lower left",
        bbox_to_anchor=(0.0, 1.01),
        ncol=3,
        frameon=False,
        fontsize=7,
        handlelength=1.1,
        handleheight=0.75,
        columnspacing=1.4,
        labelcolor=theme["ink_secondary"],
    )
    fig.text(
        0.012,
        1 - 0.30 / fig.get_figheight(),
        "Battery level by device",
        color=theme["ink"],
        fontsize=11,
        fontweight="semibold",
        va="top",
    )
    return _save(fig, path, theme)


def chart_freshness(
    devices: list[DeviceSnapshot], cfg: Settings, mode: str, path: Path
) -> Path | None:
    """Hours since each device last reported, stalest first."""
    plt = _configure_matplotlib()
    theme = THEMES[mode]

    # An exception chart, not a census: a row of a dozen 0.0h bars carries no
    # information. Devices that reported within the last hour are covered by the
    # stat tiles and the table, and are counted in the note under the title.
    candidates = [d for d in devices if d.last_seen_hours is not None]
    rows = sorted(
        (d for d in candidates if d.freshness_status != OK or (d.last_seen_hours or 0) >= 1),
        key=lambda d: (-(d.last_seen_hours or 0.0), d.name),
    )
    if not rows:
        LOG.debug("every device reported within the hour; skipping freshness chart")
        return None
    hidden = len(candidates) - len(rows)

    values = [d.last_seen_hours or 0.0 for d in rows]
    use_days = max(values) >= 48
    scaled = [v / 24.0 for v in values] if use_days else values
    unit = "days" if use_days else "hours"
    upper = max(max(scaled) * 1.08, 1.0)

    fig, ax = _new_axes(plt, len(rows), theme, right_in=1.6, top_in=1.1)
    ax.set_xlim(0, upper)
    ax.set_ylim(len(rows) - 0.5, -0.5)
    ax.set_yticks(range(len(rows)))
    ax.set_yticklabels(
        [_truncate(d.name) for d in rows], color=theme["ink_secondary"], fontsize=8
    )
    ax.set_xlabel(f"{unit} since last report", color=theme["muted"], fontsize=7.5)

    threshold = cfg.stale_hours / 24.0 if use_days else float(cfg.stale_hours)
    if threshold < upper:
        ax.axvline(threshold, color=theme["axis"], linewidth=0.8, linestyle="-", zorder=1)

    fig.canvas.draw()
    for i, (dev, value) in enumerate(zip(rows, scaled)):
        colour = STATUS_COLOR.get(dev.freshness_status, DEEMPHASIS)
        _rounded_bar(ax, i, value, 0.5, colour)
        if use_days:
            text = f"{value:.1f}d"
        elif value < 1:
            text = f"{value * 60:.0f}m"
        else:
            text = f"{value:.1f}h"
        if not dev.reachable:
            text = f"{text}  unreachable"
        elif dev.freshness_status != OK:
            text = f"{text}  stale"
        ax.annotate(
            text,
            xy=(value, i),
            xytext=(6, 0),
            textcoords="offset points",
            va="center",
            color=theme["ink"],
            fontsize=7.5,
        )

    from matplotlib.patches import Patch

    ax.legend(
        handles=[
            Patch(
                facecolor=STATUS_COLOR[CRITICAL],
                label=f"unreachable / >{cfg.stale_hours * 3}h",
            ),
            Patch(facecolor=STATUS_COLOR[WARNING], label=f"stale (>{cfg.stale_hours}h)"),
            Patch(facecolor=DEEMPHASIS, label="reporting"),
        ],
        loc="lower left",
        bbox_to_anchor=(0.0, 1.01),
        ncol=3,
        frameon=False,
        fontsize=7,
        handlelength=1.1,
        handleheight=0.75,
        columnspacing=1.4,
        labelcolor=theme["ink_secondary"],
    )
    fig.text(
        0.012,
        1 - 0.28 / fig.get_figheight(),
        "Time since last report",
        color=theme["ink"],
        fontsize=11,
        fontweight="semibold",
        va="top",
    )
    if hidden:
        fig.text(
            0.012,
            1 - 0.50 / fig.get_figheight(),
            f"{hidden} more device{'s' if hidden != 1 else ''} reported within "
            "the last hour and are not charted; see the table.",
            color=theme["ink_secondary"],
            fontsize=7.5,
            va="top",
        )
    return _save(fig, path, theme)


def render_charts(
    devices: list[DeviceSnapshot], cfg: Settings, out_dir: Path
) -> dict[str, dict[str, Path]]:
    """Render every chart in both light and dark modes."""
    charts: dict[str, dict[str, Path]] = {}
    for key, fn in (("battery", chart_battery), ("freshness", chart_freshness)):
        pair_paths: dict[str, Path] = {}
        for mode in ("light", "dark"):
            path = fn(devices, cfg, mode, out_dir / f"{key}-{mode}.png")
            if path is not None:
                pair_paths[mode] = path
        if pair_paths:
            charts[key] = pair_paths
    return charts


# --------------------------------------------------------------------------- #
# HTML report
# --------------------------------------------------------------------------- #

CSS = """
:root { color-scheme: light dark; }
body { margin:0; padding:0; background:#f9f9f7; }
.wrap { width:100%; background:#f9f9f7; }
.card { width:640px; max-width:100%; margin:0 auto; background:#fcfcfb;
        border:1px solid rgba(11,11,11,0.10); border-radius:10px; }
.pad { padding:22px 20px; }
h1 { margin:0 0 4px; font-size:19px; line-height:1.3; color:#0b0b0b;
     font-weight:600; }
.sub { margin:0; font-size:12px; color:#52514e; }
.tile-label { font-size:11px; color:#52514e; padding:0 0 3px; }
.tile-value { font-size:26px; color:#0b0b0b; font-weight:600; line-height:1.1; }
.tile-note { font-size:11px; color:#898781; padding-top:2px; }
.tile { background:#ffffff; border:1px solid rgba(11,11,11,0.10);
        border-radius:8px; padding:12px 14px; }
.alert { border-left:3px solid #d03b3b; background:rgba(208,59,59,0.06);
         padding:11px 13px; border-radius:6px; margin:0 0 6px; font-size:12px;
         color:#0b0b0b; }
.alert.warn { border-left-color:#fab219; background:rgba(250,178,25,0.10); }
.alert b { font-weight:600; }
.chart { margin:0; display:block; width:100%; max-width:600px; height:auto;
         border-radius:8px; }
table.grid { width:100%; border-collapse:collapse; font-size:11.5px; }
table.grid th { text-align:left; font-weight:600; color:#52514e; padding:7px 8px;
                border-bottom:1px solid #c3c2b7; white-space:nowrap; }
table.grid td { padding:7px 8px; border-bottom:1px solid #e1e0d9; color:#0b0b0b;
                vertical-align:top; }
table.grid td.num { text-align:right; font-variant-numeric:tabular-nums;
                    white-space:nowrap; }
.pill { font-size:10.5px; white-space:nowrap; color:#52514e; }
.dot { font-size:12px; }
h2 { margin:0 0 10px; font-size:13px; color:#0b0b0b; font-weight:600; }
.foot { font-size:11px; color:#898781; line-height:1.5; }
hr.rule { border:0; border-top:1px solid #e1e0d9; margin:22px 0; }
@media (prefers-color-scheme: dark) {
  body, .wrap { background:#0d0d0d !important; }
  .card { background:#1a1a19 !important; border-color:rgba(255,255,255,0.10) !important; }
  h1, .tile-value, .alert, table.grid td, h2 { color:#ffffff !important; }
  .sub, .tile-label, table.grid th, .pill { color:#c3c2b7 !important; }
  .tile { background:#222221 !important; border-color:rgba(255,255,255,0.10) !important; }
  .tile-note, .foot { color:#898781 !important; }
  table.grid th { border-bottom-color:#383835 !important; }
  table.grid td { border-bottom-color:#2c2c2a !important; }
  hr.rule { border-top-color:#2c2c2a !important; }
}
"""


def _esc(value: Any) -> str:
    text = "" if value is None else str(value)
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _tile(label: str, value: str, note: str = "") -> str:
    note_html = f'<div class="tile-note">{_esc(note)}</div>' if note else ""
    return (
        '<td width="33%" valign="top" style="padding:0 5px;">'
        f'<div class="tile"><div class="tile-label">{_esc(label)}</div>'
        f'<div class="tile-value">{_esc(value)}</div>{note_html}</div></td>'
    )


def _status_cell(status: str) -> str:
    glyph, word = STATUS_LABEL[status]
    colour = STATUS_COLOR.get(status, DEEMPHASIS)
    return (
        f'<span class="dot" style="color:{colour}">{glyph}</span>'
        f'&nbsp;<span class="pill">{_esc(word)}</span>'
    )


def _age_text(hours: float | None) -> str:
    if hours is None:
        return "—"
    if hours < 1:
        return f"{hours * 60:.0f}m"
    if hours < 48:
        return f"{hours:.1f}h"
    return f"{hours / 24:.1f}d"


def build_html(
    devices: list[DeviceSnapshot],
    summary: dict[str, Any],
    meta: dict[str, Any],
    cfg: Settings,
    image_srcs: dict[str, str],
) -> str:
    generated = datetime.now(timezone.utc).astimezone()
    lowest = summary["lowest_battery_percent"]

    tiles = "".join(
        [
            _tile("Devices", str(summary["devices_total"]),
                  f"{summary['devices_with_battery']} battery-powered"),
            _tile(
                "Need attention",
                str(summary["battery_critical"] + summary["battery_warning"]),
                f"{summary['battery_critical']} critical, "
                f"{summary['battery_warning']} low",
            ),
            _tile(
                "Lowest battery",
                f"{lowest}%" if lowest is not None else "—",
                summary["lowest_battery_device"] or "no battery devices",
            ),
        ]
    )

    alerts: list[str] = []
    for dev in sorted(
        devices, key=lambda d: (-SEVERITY.get(d.worst_status, 0), d.name)
    ):
        if dev.worst_status not in (WARNING, CRITICAL):
            continue
        css = "alert" if dev.worst_status == CRITICAL else "alert warn"
        alerts.append(
            f'<div class="{css}"><b>{_esc(dev.name)}</b> '
            f'<span class="pill">({_esc(dev.model)})</span><br>'
            f"{_esc('; '.join(dev.alerts))}</div>"
        )
    alerts_html = (
        f'<h2>Needs attention ({len(alerts)})</h2>{"".join(alerts)}'
        if alerts
        else '<h2>Needs attention</h2><div class="sub">Nothing outside '
        "thresholds. Every device is reporting and above the battery "
        "warning level.</div>"
    )

    charts_html = ""
    for key, title in (("battery", "Battery level"), ("freshness", "Last report")):
        light = image_srcs.get(f"{key}-light")
        if not light:
            continue
        dark = image_srcs.get(f"{key}-dark")
        img = (
            f'<img class="chart" src="{_esc(light)}" width="{EMAIL_IMAGE_WIDTH}" '
            f'alt="{_esc(title)} per device. The same values are listed in the '
            f'device table below.">'
        )
        if dark:
            img = (
                "<picture>"
                f'<source media="(prefers-color-scheme: dark)" srcset="{_esc(dark)}">'
                f"{img}</picture>"
            )
        charts_html += f'<div style="padding-bottom:18px">{img}</div>'

    rows = []
    for dev in sorted(
        devices, key=lambda d: (-SEVERITY.get(d.worst_status, 0), d.name)
    ):
        battery = (
            f"{dev.battery_percent}%" if dev.battery_percent is not None else "—"
        )
        state = "—"
        if dev.is_on is not None:
            state = "on" if dev.is_on else "off"
            if dev.dimmer is not None and dev.is_on:
                state += f" · {round(dev.dimmer / 254 * 100)}%"
        elif dev.cover_position is not None:
            state = f"{dev.cover_position}% open"
        rows.append(
            "<tr>"
            f"<td>{_esc(dev.name)}<br>"
            f'<span class="pill">{_esc(dev.model)}</span></td>'
            f'<td class="num">{_esc(battery)}</td>'
            f"<td>{_status_cell(dev.battery_status)}</td>"
            f'<td class="num">{_esc(_age_text(dev.last_seen_hours))}</td>'
            f"<td>{_status_cell(dev.freshness_status)}</td>"
            f'<td class="pill">{_esc(state)}</td>'
            f'<td class="pill">{_esc(dev.firmware or "—")}</td>'
            "</tr>"
        )

    table_html = (
        "<h2>All devices</h2>"
        '<table class="grid" role="table"><thead><tr>'
        "<th>Device</th><th>Battery</th><th>Battery status</th>"
        "<th>Last seen</th><th>Link status</th><th>State</th><th>Firmware</th>"
        "</tr></thead><tbody>" + "".join(rows) + "</tbody></table>"
    )

    demo_banner = (
        '<div class="alert warn"><b>Demo data.</b> These readings are synthetic '
        "(--demo); no gateway was contacted.</div>"
        if meta.get("demo")
        else ""
    )

    return f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<meta name="supported-color-schemes" content="light dark">
<title>{_esc(cfg.site_name)} — IKEA TRADFRI status</title>
<style>{CSS}</style>
</head>
<body>
<div style="display:none;max-height:0;overflow:hidden;opacity:0">
{_esc(_subject_tail(summary))}
</div>
<table class="wrap" role="presentation" cellpadding="0" cellspacing="0" width="100%">
<tr><td align="center" style="padding:22px 12px">
<table class="card" role="presentation" cellpadding="0" cellspacing="0">
<tr><td class="pad">
  <h1>{_esc(cfg.site_name)} — IKEA TRADFRI status</h1>
  <p class="sub">{_esc(generated.strftime('%a %d %b %Y, %H:%M %Z'))}
     &nbsp;·&nbsp; gateway {_esc(meta.get('id', 'unknown'))}
     &nbsp;·&nbsp; firmware {_esc(meta.get('firmware_version', 'unknown'))}</p>
</td></tr>
<tr><td style="padding:0 15px 18px">
  {demo_banner}
  <table role="presentation" cellpadding="0" cellspacing="0" width="100%">
    <tr>{tiles}</tr>
  </table>
</td></tr>
<tr><td style="padding:0 20px 4px">{alerts_html}</td></tr>
<tr><td style="padding:20px 20px 0">{charts_html}</td></tr>
<tr><td style="padding:0 20px">{table_html}</td></tr>
<tr><td class="pad">
  <hr class="rule">
  <p class="foot">
    Thresholds: critical at or below {cfg.battery_critical}% ·
    low at or below {cfg.battery_warning}% ·
    stale after {cfg.stale_hours}h without a report.<br>
    Generated by ikea_energy.py. Chart colours are validated for
    colour-vision deficiency; every charted value also appears in the table.
  </p>
</td></tr>
</table>
</td></tr></table>
</body></html>
"""


def _subject_tail(summary: dict[str, Any]) -> str:
    parts = [f"{summary['devices_total']} devices"]
    if summary["battery_critical"]:
        parts.append(f"{summary['battery_critical']} critical battery")
    if summary["battery_warning"]:
        parts.append(f"{summary['battery_warning']} low battery")
    if summary["unreachable"]:
        parts.append(f"{summary['unreachable']} unreachable")
    if len(parts) == 1:
        parts.append("all healthy")
    return ", ".join(parts)


def build_text(
    devices: list[DeviceSnapshot],
    summary: dict[str, Any],
    meta: dict[str, Any],
    cfg: Settings,
) -> str:
    generated = datetime.now(timezone.utc).astimezone()
    lines = [
        f"{cfg.site_name} - IKEA TRADFRI status",
        generated.strftime("%a %d %b %Y, %H:%M %Z"),
        f"gateway {meta.get('id', 'unknown')} "
        f"(firmware {meta.get('firmware_version', 'unknown')})",
        "",
        _subject_tail(summary),
        "",
    ]
    if meta.get("demo"):
        lines += ["** DEMO DATA - no gateway was contacted **", ""]

    flagged = [d for d in devices if d.worst_status in (WARNING, CRITICAL)]
    if flagged:
        lines.append("NEEDS ATTENTION")
        for dev in sorted(
            flagged, key=lambda d: (-SEVERITY.get(d.worst_status, 0), d.name)
        ):
            lines.append(f"  [{dev.worst_status.upper()}] {dev.name} ({dev.model})")
            for alert in dev.alerts:
                lines.append(f"      - {alert}")
    else:
        lines.append("NEEDS ATTENTION: none")
    lines.append("")

    lines.append("ALL DEVICES")
    width = max((len(d.name) for d in devices), default=6)
    for dev in sorted(devices, key=lambda d: d.name):
        battery = (
            f"{dev.battery_percent:>3}%" if dev.battery_percent is not None else "  --"
        )
        lines.append(
            f"  {dev.name:<{width}}  {battery}  "
            f"{STATUS_LABEL[dev.battery_status][1]:<8}  "
            f"seen {_age_text(dev.last_seen_hours):>6}  "
            f"{'' if dev.reachable else 'UNREACHABLE'}".rstrip()
        )
    lines += [
        "",
        f"Thresholds: critical <={cfg.battery_critical}%, "
        f"low <={cfg.battery_warning}%, stale >{cfg.stale_hours}h.",
        "Generated by ikea_energy.py",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# E-mail delivery
# --------------------------------------------------------------------------- #


def _validate_email_settings(cfg: Settings) -> None:
    if cfg.email_backend == "none":
        raise ConfigError(
            "EMAIL_BACKEND=none, so --email is refused. "
            "Set EMAIL_BACKEND=smtp or ses, or use --json / --html-out."
        )
    if not cfg.email_from:
        raise ConfigError("EMAIL_FROM is not set")
    if not cfg.email_to:
        raise ConfigError("EMAIL_TO is not set")
    if cfg.email_backend == "smtp":
        if not cfg.smtp_host:
            raise ConfigError("SMTP_HOST is not set")
        if not cfg.smtp_username or not cfg.smtp_password:
            raise ConfigError(
                "SMTP_USERNAME and SMTP_PASSWORD are required for "
                "EMAIL_BACKEND=smtp.\nFor Gmail this must be an App Password "
                "(https://myaccount.google.com/apppasswords), not your account "
                "password."
            )


def build_message(
    devices: list[DeviceSnapshot],
    summary: dict[str, Any],
    meta: dict[str, Any],
    cfg: Settings,
    charts: dict[str, dict[str, Path]],
) -> EmailMessage:
    """multipart/alternative(text, related(html + inline PNGs))."""
    cids: dict[str, str] = {}
    srcs: dict[str, str] = {}
    for key, modes in charts.items():
        for mode, path in modes.items():
            cid = make_msgid(domain="ikea-energy.invalid")
            cids[f"{key}-{mode}"] = cid
            srcs[f"{key}-{mode}"] = f"cid:{cid[1:-1]}"

    msg = EmailMessage()
    msg["Subject"] = f"{cfg.subject_prefix} {cfg.site_name}: {_subject_tail(summary)}".strip()
    msg["From"] = (
        formataddr((cfg.email_from_name, cfg.email_from))
        if cfg.email_from_name
        else cfg.email_from
    )
    msg["To"] = ", ".join(cfg.email_to)
    if cfg.email_cc:
        msg["Cc"] = ", ".join(cfg.email_cc)
    if cfg.email_reply_to:
        msg["Reply-To"] = ", ".join(cfg.email_reply_to)
    if summary["worst_status"] == CRITICAL:
        msg["X-Priority"] = "2"

    msg.set_content(build_text(devices, summary, meta, cfg))
    msg.add_alternative(
        build_html(devices, summary, meta, cfg, srcs), subtype="html"
    )

    html_part = msg.get_payload()[-1]
    for name, cid in cids.items():
        key, mode = name.rsplit("-", 1)
        html_part.add_related(
            charts[key][mode].read_bytes(),
            maintype="image",
            subtype="png",
            cid=cid,
            filename=f"{name}.png",
        )
    return msg


def send_email(msg: EmailMessage, cfg: Settings) -> None:
    recipients = cfg.email_to + cfg.email_cc
    if cfg.email_backend == "smtp":
        _send_smtp(msg, cfg, recipients)
    else:
        _send_ses(msg, cfg, recipients)
    LOG.info("sent %r to %s", msg["Subject"], ", ".join(recipients))


def _send_smtp(msg: EmailMessage, cfg: Settings, recipients: list[str]) -> None:
    LOG.info("connecting to %s:%s", cfg.smtp_host, cfg.smtp_port)
    try:
        if cfg.smtp_starttls:
            with smtplib.SMTP(
                cfg.smtp_host, cfg.smtp_port, timeout=cfg.smtp_timeout
            ) as server:
                server.ehlo()
                server.starttls()
                server.ehlo()
                server.login(cfg.smtp_username, cfg.smtp_password)
                server.send_message(msg, cfg.email_from, recipients)
        else:
            with smtplib.SMTP_SSL(
                cfg.smtp_host, cfg.smtp_port, timeout=cfg.smtp_timeout
            ) as server:
                server.login(cfg.smtp_username, cfg.smtp_password)
                server.send_message(msg, cfg.email_from, recipients)
    except smtplib.SMTPAuthenticationError as exc:
        raise RuntimeError(
            f"SMTP rejected the credentials: {exc}\n"
            "For Gmail: enable 2-Step Verification and use a 16-character App "
            "Password in SMTP_PASSWORD "
            "(https://myaccount.google.com/apppasswords). Workspace accounts "
            "with SSO may have app passwords disabled by policy -- use SES."
        ) from exc
    except (smtplib.SMTPException, OSError) as exc:
        raise RuntimeError(
            f"SMTP delivery to {cfg.smtp_host}:{cfg.smtp_port} failed: {exc}\n"
            "Check SMTP_PORT/SMTP_STARTTLS agree: 587 needs "
            "SMTP_STARTTLS=true, 465 needs SMTP_STARTTLS=false."
        ) from exc


def _send_ses(msg: EmailMessage, cfg: Settings, recipients: list[str]) -> None:
    try:
        import boto3
        from botocore.exceptions import BotoCoreError, ClientError
    except ImportError:
        raise RuntimeError(
            "EMAIL_BACKEND=ses needs boto3. Run: pip install boto3"
        ) from None

    session_kwargs: dict[str, Any] = {}
    if cfg.aws_profile:
        session_kwargs["profile_name"] = cfg.aws_profile
    if cfg.aws_region:
        session_kwargs["region_name"] = cfg.aws_region
    if cfg.aws_access_key_id and cfg.aws_secret_access_key:
        session_kwargs["aws_access_key_id"] = cfg.aws_access_key_id
        session_kwargs["aws_secret_access_key"] = cfg.aws_secret_access_key
    else:
        LOG.debug("no explicit AWS keys; using the ambient credential chain")

    client = boto3.session.Session(**session_kwargs).client("sesv2")
    request: dict[str, Any] = {
        "FromEmailAddress": msg["From"],
        "Destination": {"ToAddresses": cfg.email_to},
        "Content": {"Raw": {"Data": msg.as_bytes()}},
    }
    if cfg.email_cc:
        request["Destination"]["CcAddresses"] = cfg.email_cc
    if cfg.ses_configuration_set:
        request["ConfigurationSetName"] = cfg.ses_configuration_set

    try:
        response = client.send_email(**request)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        hint = ""
        if code in {"MessageRejected", "NotFoundException"}:
            hint = (
                f"\nSES is region-scoped: verify {cfg.email_from!r} in "
                f"{cfg.aws_region or 'the configured region'}, and confirm the "
                "account is out of the sandbox (in the sandbox, recipients must "
                "be verified too)."
            )
        raise RuntimeError(f"SES rejected the message ({code}): {exc}{hint}") from exc
    except BotoCoreError as exc:
        raise RuntimeError(f"SES call failed: {exc}") from exc
    LOG.debug("SES MessageId %s", response.get("MessageId"))


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="ikea_energy.py",
        description="Battery and status report for an IKEA TRADFRI gateway.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  python ikea_energy.py --pair\n"
            "  python ikea_energy.py --json\n"
            "  python ikea_energy.py --email\n"
            "  python ikea_energy.py --demo --html-out out/report.html\n"
            "  python ikea_energy.py --json --fail-on-alert   # exit 2 if critical\n"
        ),
    )
    out = parser.add_argument_group("output")
    out.add_argument(
        "--json",
        action="store_true",
        help="write a JSON snapshot to stdout (the default when nothing else "
        "is requested)",
    )
    out.add_argument(
        "--email", action="store_true", help="send the HTML chart report by e-mail"
    )
    out.add_argument(
        "--html-out",
        metavar="PATH",
        help="write the HTML report and chart PNGs to PATH instead of sending",
    )
    src = parser.add_argument_group("source")
    src.add_argument(
        "--demo",
        action="store_true",
        help="use built-in synthetic devices; contacts no gateway",
    )
    src.add_argument(
        "--pair",
        action="store_true",
        help="exchange TRADFRI_SECURITY_CODE for a PSK, print it, and exit",
    )
    src.add_argument("--env-file", metavar="PATH", help="read configuration from PATH")
    misc = parser.add_argument_group("misc")
    misc.add_argument(
        "--fail-on-alert",
        action="store_true",
        help="exit 2 when any device is critical (for cron and monitoring)",
    )
    misc.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    misc.add_argument(
        "-q", "--quiet", action="store_true", help="warnings and errors only"
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG
        if args.verbose
        else logging.WARNING
        if args.quiet
        else logging.INFO,
        format="%(levelname)-7s %(message)s",
        stream=sys.stderr,  # keep stdout pure JSON
    )

    try:
        cfg = Settings.load(args.env_file)

        if args.pair:
            psk = pair(cfg)
            LOG.info("pairing succeeded")
            print(
                "\nAdd these two lines to .env (and clear "
                "TRADFRI_SECURITY_CODE afterwards):\n"
                f"\n  TRADFRI_IDENTITY={cfg.identity}"
                f"\n  TRADFRI_PSK={psk}\n"
            )
            return 0

        if args.demo:
            devices, meta = demo_devices()
            LOG.info("using %d synthetic demo devices", len(devices))
        else:
            devices, meta = read_gateway(cfg)
            LOG.info("read %d devices", len(devices))

        if not devices:
            raise GatewayError(
                "The gateway returned no devices. Is anything paired to it?"
            )

        classify(devices, cfg)
        summary = summarise(devices, cfg)

        wants_report = args.email or args.html_out
        if not wants_report and not args.json:
            args.json = True

        if wants_report:
            out_dir = (
                Path(args.html_out).parent if args.html_out else cfg.output_dir
            )
            charts = render_charts(devices, cfg, out_dir)

            if args.html_out:
                target = Path(args.html_out)
                srcs = {
                    f"{key}-{mode}": os.path.relpath(path, target.parent).replace(
                        os.sep, "/"
                    )
                    for key, modes in charts.items()
                    for mode, path in modes.items()
                }
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(
                    build_html(devices, summary, meta, cfg, srcs), encoding="utf-8"
                )
                LOG.info("wrote %s", target)

            if args.email:
                _validate_email_settings(cfg)
                send_email(
                    build_message(devices, summary, meta, cfg, charts), cfg
                )

        if args.json:
            json.dump(
                {
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                    "site": cfg.site_name,
                    "gateway": meta,
                    "summary": summary,
                    "devices": [d.to_dict() for d in devices],
                },
                sys.stdout,
                indent=2,
            )
            sys.stdout.write("\n")

        if args.fail_on_alert and summary["worst_status"] == CRITICAL:
            LOG.warning("exiting 2: at least one device is critical")
            return 2
        return 0

    except (ConfigError, GatewayError) as exc:
        LOG.error("%s", exc)
        return 1
    except RuntimeError as exc:
        LOG.error("%s", exc)
        return 1
    except KeyboardInterrupt:
        LOG.warning("interrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
