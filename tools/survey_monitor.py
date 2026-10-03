#!/usr/bin/env python3
"""Read-only VCP survey, used to produce the files in ``docs/monitors/``.

Why this is not part of the application: the app exposes a deliberately small
allowlist of features.  Documenting a monitor needs the opposite — reading every
code the standard defines, including ones the app will never write, to find out
what the firmware actually implements.

Safety properties, in order of importance:

* **Reads only.**  No ``SetVCPFeature`` call exists anywhere in this file.
* **The manufacturer-reserved range 0xE0-0xFF is never touched**, not even to
  read, matching the project's write policy.
* **Generous pacing** between transactions.  The bus has been observed to wedge
  under tighter timing, and a survey issues far more transactions than normal
  use.

Usage::

    python tools/survey_monitor.py                    # all monitors, table
    python tools/survey_monitor.py --monitor 1        # one monitor
    python tools/survey_monitor.py --json survey.json # machine-readable too
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from monitorctl.ddc.buslock import BusLock
from monitorctl.ddc.capabilities import parse_capabilities
from monitorctl.ddc.errors import DdcError, DdcUnsupportedFeature
from monitorctl.ddc.policy import (
    MANUFACTURER_RESERVED_END,
    MANUFACTURER_RESERVED_START,
)
from monitorctl.ddc.windows import WindowsDdcBackend

#: Seconds between transactions.  Deliberately well above the DDC minimum.
DEFAULT_PACE = 0.15

#: MCCS 2.2a feature names.
VCP_NAMES: dict[int, str] = {
    0x01: "Degauss",
    0x02: "New control value",
    0x03: "Soft controls",
    0x04: "Restore factory defaults",
    0x05: "Restore factory brightness/contrast defaults",
    0x06: "Restore factory geometry defaults",
    0x08: "Restore color defaults",
    0x0A: "Restore factory TV defaults",
    0x0B: "Color temperature increment",
    0x0C: "Color temperature request",
    0x0E: "Clock",
    0x10: "Brightness",
    0x11: "Flesh tone enhancement",
    0x12: "Contrast",
    0x13: "Backlight control",
    0x14: "Select color preset",
    0x16: "Video gain: Red",
    0x17: "User color vision compensation",
    0x18: "Video gain: Green",
    0x1A: "Video gain: Blue",
    0x1C: "Focus",
    0x1E: "Auto setup",
    0x1F: "Auto color setup",
    0x20: "Horizontal position",
    0x22: "Horizontal size",
    0x24: "Horizontal pincushion",
    0x26: "Horizontal pincushion balance",
    0x28: "Horizontal convergence",
    0x2A: "Horizontal linearity",
    0x2C: "Horizontal linearity balance",
    0x2E: "Gray scale expansion",
    0x30: "Vertical position",
    0x32: "Vertical size",
    0x34: "Vertical pincushion",
    0x36: "Vertical pincushion balance",
    0x38: "Vertical convergence",
    0x3A: "Vertical linearity",
    0x3C: "Vertical linearity balance",
    0x3E: "Clock phase",
    0x40: "Key balance",
    0x41: "Vertical parallelogram",
    0x42: "Horizontal trapezoid",
    0x43: "Vertical trapezoid",
    0x44: "Tilt (rotation)",
    0x46: "Top corner",
    0x48: "Top corner balance",
    0x4A: "Bottom corner",
    0x4C: "Bottom corner balance",
    0x52: "Active control",
    0x54: "Performance preservation",
    0x56: "Horizontal moire",
    0x58: "Vertical moire",
    0x59: "6-axis saturation: Red",
    0x5A: "6-axis saturation: Yellow",
    0x5B: "6-axis saturation: Green",
    0x5C: "6-axis saturation: Cyan",
    0x5D: "6-axis saturation: Blue",
    0x5E: "6-axis saturation: Magenta",
    0x60: "Input source",
    0x62: "Audio speaker volume",
    0x63: "Speaker select",
    0x64: "Audio: microphone volume",
    0x66: "Ambient light sensor",
    0x6C: "Video black level: Red",
    0x6E: "Video black level: Green",
    0x70: "Video black level: Blue",
    0x72: "Gamma",
    0x73: "LUT size",
    0x76: "Remote procedure call",
    0x78: "EDID operation",
    0x7C: "Adjust zoom",
    0x82: "Horizontal flip",
    0x84: "Vertical flip",
    0x86: "Display scaling",
    0x87: "Sharpness",
    0x88: "Velocity scan modulation",
    0x8A: "TV color saturation",
    0x8D: "Audio mute / screen blank",
    0x8F: "Audio treble",
    0x90: "Hue",
    0x91: "Audio bass",
    0x93: "Audio balance L/R",
    0x94: "Audio processor mode",
    0x9B: "6-axis hue: Red",
    0x9C: "6-axis hue: Yellow",
    0x9D: "6-axis hue: Green",
    0x9E: "6-axis hue: Cyan",
    0x9F: "6-axis hue: Blue",
    0xA0: "6-axis hue: Magenta",
    0xA2: "Auto setup on/off",
    0xAA: "Screen orientation",
    0xAC: "Horizontal frequency",
    0xAE: "Vertical frequency",
    0xB0: "Settings (store/restore)",
    0xB2: "Flat panel sub-pixel layout",
    0xB4: "Source timing mode",
    0xB6: "Display technology type",
    0xB7: "Monitor status",
    0xBE: "Link control",
    0xC0: "Display usage time",
    0xC2: "Display descriptor length",
    0xC6: "Application enable key",
    0xC8: "Display controller type",
    0xC9: "Display firmware level",
    0xCA: "OSD / button control",
    0xCC: "OSD language",
    0xCD: "Status indicators",
    0xD0: "Output select",
    0xD2: "Asset tag",
    0xD4: "Stereo video mode",
    0xD6: "Power mode (DPMS)",
    0xDA: "Scan mode",
    0xDB: "Image mode",
    0xDC: "Display application / picture mode",
    0xDF: "VCP version",
}

#: Probed even when the capabilities string omits them.  Both monitors surveyed
#: so far implement codes they do not advertise, so a survey that trusted the
#: capabilities string would have missed volume, sharpness and power mode.
PROBE_ANYWAY: tuple[int, ...] = (
    0x02,
    0x0B,
    0x0C,
    0x0E,
    0x10,
    0x12,
    0x14,
    0x16,
    0x18,
    0x1A,
    0x1E,
    0x20,
    0x30,
    0x3E,
    0x52,
    0x60,
    0x62,
    0x66,
    0x6C,
    0x6E,
    0x70,
    0x72,
    0x86,
    0x87,
    0x8A,
    0x8D,
    0x90,
    0xAA,
    0xAC,
    0xAE,
    0xB2,
    0xB6,
    0xC0,
    0xC6,
    0xC8,
    0xC9,
    0xCA,
    0xCC,
    0xD0,
    0xD6,
    0xDA,
    0xDB,
    0xDC,
    0xDF,
)


def _is_reserved(code: int) -> bool:
    return MANUFACTURER_RESERVED_START <= code <= MANUFACTURER_RESERVED_END


def survey_one(
    backend: WindowsDdcBackend, monitor: Any, pace: float, bus: BusLock
) -> dict[str, Any]:
    """Read the capabilities string and every non-reserved candidate code.

    Every transaction holds the cross-process bus lock, pause included, so a
    survey can run while the tray app is up without interleaving with it.
    """
    try:
        with bus:
            time.sleep(pace)
            raw = backend.read_capabilities(monitor)
    except DdcError as exc:
        print(f"aviso: capabilities ilegivel em {monitor.device}: {exc}", file=sys.stderr)
        raw = ""
    caps = parse_capabilities(raw)

    entry: dict[str, Any] = {
        "device": monitor.device,
        "description": monitor.description,
        "primary": monitor.primary,
        "capabilities_raw": raw,
        "model": caps.model,
        "display_type": caps.display_type,
        "mccs_version": caps.mccs_version,
        "commands": [f"0x{c:02X}" for c in sorted(caps.commands)],
        "capability_warnings": list(caps.warnings),
        "reserved_skipped": [f"0x{c:02X}" for c in sorted(caps.vcp) if _is_reserved(c)],
        "features": [],
    }

    candidates = sorted(set(caps.vcp) | set(PROBE_ANYWAY))
    for code in candidates:
        if _is_reserved(code):
            continue  # never touched, not even read
        record: dict[str, Any] = {
            "code": f"0x{code:02X}",
            "name": VCP_NAMES.get(code, "?"),
            "advertised": code in caps.vcp,
            "advertised_values": [f"0x{v:02X}" for v in caps.vcp.get(code, ())],
        }
        try:
            with bus:
                time.sleep(pace)
                reading = backend.get_vcp(monitor, code)
        except DdcUnsupportedFeature:
            record["status"] = "unsupported"
        except DdcError as exc:
            record["status"] = "error"
            record["error"] = f"{type(exc).__name__}: {exc}"
        else:
            record["status"] = "ok"
            record["current"] = reading.current
            record["maximum"] = reading.maximum
            # A current value above the reported maximum proves the maximum is
            # meaningless for this code -- common for enumerated and read-only
            # features.  Worth flagging in the generated documentation.
            record["maximum_suspect"] = reading.current > reading.maximum
        entry["features"].append(record)

    return entry


def print_report(entries: list[dict[str, Any]]) -> None:
    """Print a table close to the layout used in ``docs/monitors/``."""
    for entry in entries:
        name = entry["model"] or entry["description"] or entry["device"]
        print("=" * 76)
        print(f"{name}   {entry['device']}")
        mccs = entry["mccs_version"] or "?"
        kind = entry["display_type"] or "?"
        print(f"  MCCS {mccs}   tipo {kind}")
        for warning in entry["capability_warnings"]:
            print(f"  aviso: {warning}")
        if entry["reserved_skipped"]:
            print(f"  NAO LIDO (faixa reservada): {', '.join(entry['reserved_skipped'])}")
        print(f"  capabilities: {entry['capabilities_raw'] or '(vazia)'}")
        print("-" * 76)
        print(f"  {'cod':5} {'anun':5} {'atual':>7} {'max':>7}  nome")
        for f in entry["features"]:
            if f["status"] == "unsupported":
                continue
            advertised = "sim" if f["advertised"] else "-"
            if f["status"] == "ok":
                current, maximum = f["current"], f["maximum"]
                flag = " (!)" if f.get("maximum_suspect") else ""
            else:
                current, maximum, flag = "ERRO", "", ""
            print(
                f"  {f['code']:5} {advertised:5} {current!s:>7} "
                f"{maximum!s:>7}  {f['name']}{flag}"
            )
        unsupported = [
            f["code"] for f in entry["features"] if f["status"] == "unsupported"
        ]
        if unsupported:
            print(f"  nao suportados: {', '.join(unsupported)}")
        print()
    print("(!) = valor atual maior que o maximo reportado; o maximo nao vale nada")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Levantamento somente-leitura dos codigos VCP de cada monitor. "
            "A faixa reservada ao fabricante (0xE0-0xFF) nunca e tocada."
        )
    )
    parser.add_argument(
        "--monitor", type=int, help="posicao do monitor (1, 2, ...); padrao: todos"
    )
    parser.add_argument("--json", type=Path, help="tambem grava o resultado em JSON")
    parser.add_argument(
        "--pace",
        type=float,
        default=DEFAULT_PACE,
        help=f"segundos entre transacoes (padrao {DEFAULT_PACE}); "
        "reduzir arrisca travar o barramento",
    )
    args = parser.parse_args(argv)

    if args.pace < 0.05:
        parser.error("--pace abaixo de 0.05 s e imprudente para um levantamento")

    if sys.platform != "win32":
        print("erro: so ha backend para Windows", file=sys.stderr)
        return 2

    backend = WindowsDdcBackend()
    bus = BusLock()
    try:
        monitors = backend.enumerate()
        if not monitors:
            print("Nenhum monitor com DDC/CI detectado.", file=sys.stderr)
            return 1
        if args.monitor is not None:
            if not 1 <= args.monitor <= len(monitors):
                print(
                    f"erro: monitor {args.monitor} fora da faixa 1..{len(monitors)}",
                    file=sys.stderr,
                )
                return 2
            monitors = [monitors[args.monitor - 1]]

        entries = [survey_one(backend, m, args.pace, bus) for m in monitors]
    finally:
        backend.close()
        bus.close()

    print_report(entries)

    if args.json:
        args.json.write_text(
            json.dumps(entries, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print(f"\nJSON gravado em {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
