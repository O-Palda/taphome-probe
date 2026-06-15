#!/usr/bin/env python3
# taphome_probe.py — TapHome probe + martindybal config linter.
# Copyright (C) 2026  O-Palda
# SPDX-License-Identifier: GPL-3.0-or-later
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation, either version 3 of the License, or (at your option) any later
# version. This program is distributed WITHOUT ANY WARRANTY. See the LICENSE
# file or <https://www.gnu.org/licenses/> for the full text.
#
# Built on the official TapHome API by TapHome:
#   https://taphome.com/en/docs/expose-devices/taphome-api/
# "TapHome" is a trademark of its respective owner. This is an independent,
# unofficial tool, not affiliated with or endorsed by TapHome. The device
# classification logic was reimplemented independently from the open-source
# Home Assistant integrations credited in the README.
"""TapHome probe + martindybal config linter.

Talks to the TapHome local API (same endpoint both HA integrations use) and:

  probe : list every device the Core exposes, with the detected type and the
          martindybal YAML section it belongs in.
  init  : generate a complete, commented martindybal config from the Core when
          you don't have one yet (placeholders for token / api_url / webhook_id).
  compare : compare the Core's devices against an existing martindybal
          configuration.yaml and report devices that are MISPLACED (listed in
          the wrong section), NOT SET UP (exposed but absent from the config),
          or STALE (referenced in the config but no longer on the Core).

The device-type rules are lifted from martindybal's taphome_sdk/device_factory.py
and cross-checked against khral07's per-platform discovery filters, so the
classification matches what both integrations actually do at runtime.

Stdlib only, except PyYAML for the compare mode (pip install pyyaml).

Examples
--------
  # Inventory straight from the Core:
  python taphome_probe.py probe --url http://192.168.1.3/api/TapHomeApi/v1 --token YOURTOKEN

  # Save discovery to a file (run on the HA host), compare elsewhere:
  python taphome_probe.py probe --url http://192.168.1.3/api/TapHomeApi/v1 --token YOURTOKEN --json discovery.json
  python taphome_probe.py compare --discovery discovery.json --config configuration.yaml

  # One shot, live:
  python taphome_probe.py compare --url http://192.168.1.3/api/TapHomeApi/v1 --token YOURTOKEN --config configuration.yaml
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.request
import urllib.error

# --------------------------------------------------------------------------- #
# TapHome value type ids (subset, from taphome_sdk/value_type.py).             #
# --------------------------------------------------------------------------- #
V = {
    3: "Humidity", 4: "CO2", 5: "Temperature", 6: "TargetTemperature",
    10: "BlindsSlope(tilt)", 15: "WindDirection", 19: "AlarmMode",
    20: "AlarmState", 24: "CO2", 37: "ValveState", 40: "Hue", 41: "Saturation",
    42: "AnalogOutput", 43: "WindowOpen", 44: "ReedContact", 45: "Smoke",
    46: "BlindsLevel", 47: "Flood", 48: "SwitchState", 49: "MultiValueSwitch",
    51: "Motion", 52: "Button", 62: "Variable", 65: "HueBrightness",
    66: "BlindsIsMoving", 67: "AnalogDesired", 69: "Clock", 71: "MultiValueSet",
    75: "Energy", 77: "Power", 89: "ColorTemp(CCT)", 90: "LightIntensity",
}

# Value types that read as a binary/contact sensor.
BINARY_TYPES = {43, 44, 45, 47, 51, 63, 64}
# "Pure sensor" read-only measurement types (no actuator).
SENSOR_TYPES = {2, 3, 4, 5, 13, 14, 15, 24, 25, 26, 54, 56, 59, 60, 62,
                73, 75, 77, 78, 82, 84, 87, 88, 90}

# Suggested sensor metadata per value type, emitted (commented) by `init` so you
# can uncomment and tweak. (valueTypeId, device_class, unit, state_class).
SENSOR_HINTS = [
    (5,  "temperature",                 "°C",   "measurement"),
    (3,  "humidity",                    "%",    "measurement"),
    (24, "carbon_dioxide",              "ppm",  "measurement"),
    (4,  "carbon_dioxide",              "ppm",  "measurement"),
    (90, "illuminance",                 "lx",   "measurement"),
    (2,  "illuminance",                 "lx",   "measurement"),
    (77, "power",                       "W",    "measurement"),
    (75, "energy",                      "kWh",  "total_increasing"),
    (87, "voltage",                     "V",    "measurement"),
    (88, "current",                     "A",    "measurement"),
    (25, "pressure",                    "hPa",  "measurement"),
    (54, "volatile_organic_compounds",  "µg/m³", "measurement"),
    (14, None,                          "km/h", "measurement"),   # wind speed
    (73, "battery",                     "%",    "measurement"),
]
# Suggested binary_sensor device_class per value type.
BINARY_HINTS = [
    (45, "smoke"), (47, "moisture"), (51, "motion"),
    (43, "window"), (44, "door"), (63, "safety"), (64, "occupancy"),
]


def sensor_hint(S):
    for tid, dc, unit, sc in SENSOR_HINTS:
        if tid in S:
            return dc, unit, sc
    return None


def binary_hint(S):
    for tid, dc in BINARY_HINTS:
        if tid in S:
            return dc
    return None


# Known martindybal config sections (everything else in a core block is meta).
KNOWN_SECTIONS = {
    "lights", "switches", "sensors", "covers", "binary_sensors", "climates",
    "fans", "humidifiers", "valves", "buttons", "multivalue_switches", "times",
}

# --------------------------------------------------------------------------- #
# Capability predicates (S = set of supported valueTypeIds for a device).      #
# --------------------------------------------------------------------------- #
def cap_cover(S):       return 46 in S and 66 in S          # BidirectionalDevice
def cap_thermostat(S):  return 6 in S                       # ThermostatDevice
def cap_switch(S):      return 48 in S                      # DigitalOutputDevice
def cap_analog(S):      return 42 in S                      # AnalogOutputDevice
def cap_multivalue(S):  return 49 in S                      # MultiValueSwitchDevice
def cap_button(S):      return 52 in S                      # ButtonDevice
def cap_genout(S):      return 48 in S or 42 in S           # fan/valve generic output
def cap_light(S):       return 48 in S or 42 in S or 65 in S
def cap_humidity(S):    return 3 in S
def cap_any(S):         return True

# Role required by each id-bearing field name inside a config entry.
# (field_name -> (predicate, human description of what it must be))
ROLE_FIELDS = {
    "range_high_thermostat_id": (cap_thermostat, "a thermostat (TargetTemperature)"),
    "range_low_thermostat_id":  (cap_thermostat, "a thermostat (TargetTemperature)"),
    "hvac_switch_id":           (cap_switch,     "a switch (SwitchState)"),
    "hvac_mode_id":             (cap_multivalue, "a multi-value switch"),
    "hvac_action_id":           (cap_multivalue, "a multi-value switch"),
    "preset_mode_id":           (cap_multivalue, "a multi-value switch"),
    "fan_mode_id":              (cap_multivalue, "a multi-value switch"),
    "swing_mode_id":            (cap_multivalue, "a multi-value switch"),
    "swing_horizontal_mode_id": (cap_multivalue, "a multi-value switch"),
    "mode_id":                  (cap_multivalue, "a multi-value switch"),
    "action_id":                (cap_multivalue, "a multi-value switch"),
    "effect_id":                (cap_multivalue, "a multi-value switch"),
    "target_humidity_id":       (cap_analog,     "an analog output"),
    "switch_id":                (cap_switch,     "a switch (SwitchState)"),
    "humidity_sensor_id":       (cap_humidity,   "a humidity sensor"),
}

# Primary `id` requirement per section.
SECTION_PRIMARY = {
    "lights":              (cap_light,      "an on/off, dimmable or colour light"),
    "switches":            (cap_switch,     "a switch (SwitchState)"),
    "covers":              (cap_cover,      "a blind (BlindsLevel + BlindsIsMoving)"),
    "climates":            (cap_thermostat, "a thermostat (TargetTemperature)"),
    "fans":                (cap_genout,     "a generic output (switch or analog)"),
    "valves":              (cap_genout,     "a generic output (switch or analog)"),
    "buttons":             (cap_button,     "a button (Button)"),
    "multivalue_switches": (cap_multivalue, "a multi-value switch"),
    "sensors":             (cap_any,        "any device"),
    "binary_sensors":      (cap_any,        "any device"),
    "times":               (cap_any,        "any device"),
}


def suggest_section(S):
    """Best-guess martindybal section for a device, priority like device_factory."""
    if cap_cover(S):                                   return "covers"
    if cap_thermostat(S):                              return "climates"
    if 42 in S or 65 in S or 89 in S or {40, 41} <= S: return "lights"
    if cap_multivalue(S):                              return "multivalue_switches"
    if cap_switch(S):                                  return "switches"   # could also be a light
    if cap_button(S):                                  return "buttons"
    if S & BINARY_TYPES:                               return "binary_sensors"
    if S & SENSOR_TYPES:                               return "sensors"
    return "sensors"


def valid_sections(S):
    """All sections a device legitimately fits in (for suggestions)."""
    out = []
    for sec, (pred, _) in SECTION_PRIMARY.items():
        if sec in ("sensors", "binary_sensors", "times"):
            continue
        if pred(S):
            out.append(sec)
    if S & BINARY_TYPES:
        out.append("binary_sensors")
    out.append("sensors")  # any device can be exposed as a sensor
    return out


def is_readonly(S):
    """Device with no actuator capability (pure sensor / binary sensor)."""
    return not (cap_switch(S) or cap_analog(S) or cap_cover(S) or
                cap_thermostat(S) or cap_multivalue(S) or cap_button(S) or 65 in S)


# --------------------------------------------------------------------------- #
# HTTP / discovery                                                            #
# --------------------------------------------------------------------------- #
def fetch_discovery(url, token, timeout=10):
    endpoint = url.rstrip("/") + "/discovery"
    req = urllib.request.Request(
        endpoint,
        headers={"Authorization": f"TapHome {token}",
                 "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 401:
            sys.exit("ERROR: 401 Unauthorized — check the API token "
                     "(TapHome → Settings → Expose Devices → Token).")
        sys.exit(f"ERROR: HTTP {e.code} from {endpoint}: {e.reason}")
    except urllib.error.URLError as e:
        sys.exit(f"ERROR: cannot reach {endpoint}: {e.reason}")


def devices_from_discovery(data):
    """Normalise discovery payload -> list of device dicts."""
    if isinstance(data, dict) and "devices" in data:
        data = data["devices"]
    if not isinstance(data, list):
        sys.exit("ERROR: unexpected discovery payload shape.")
    out = []
    for d in data:
        S = {sv.get("valueTypeId") for sv in d.get("supportedValues", [])}
        S.discard(None)
        out.append({
            "id": d.get("deviceId"),
            "name": d.get("name", "?"),
            "zone": d.get("zone") or d.get("category") or "",
            "S": S,
        })
    return out


# --------------------------------------------------------------------------- #
# YAML config parsing                                                         #
# --------------------------------------------------------------------------- #
def load_config_refs(path):
    """Return list of references: {id, section, field} for every device id in the config."""
    try:
        import yaml
    except ImportError:
        sys.exit("ERROR: PyYAML required for compare mode -> pip install pyyaml")

    # Home Assistant configs use custom tags (!secret, !include, !env_var, ...)
    # that safe_load can't construct. Ignore any unknown tag -> None, since we
    # only care about the device-id sections, not secrets/includes.
    class _HALoader(yaml.SafeLoader):
        pass

    _HALoader.add_multi_constructor("!", lambda loader, suffix, node: None)

    with open(path, "r", encoding="utf-8") as f:
        doc = yaml.load(f, Loader=_HALoader)

    # Locate the taphome block, then the list of cores.
    th = doc.get("taphome", doc) if isinstance(doc, dict) else doc
    if isinstance(th, dict) and "cores" in th:
        cores = th["cores"]
    elif isinstance(th, dict):
        cores = [th]            # single-core / legacy flat layout
    elif isinstance(th, list):
        cores = th
    else:
        cores = []

    refs = []
    for core in cores:
        if not isinstance(core, dict):
            continue
        for section, items in core.items():
            if section not in KNOWN_SECTIONS or not isinstance(items, list):
                continue
            for item in items:
                if isinstance(item, int):
                    refs.append({"id": item, "section": section, "field": "id"})
                elif isinstance(item, dict):
                    for key, val in item.items():
                        if not isinstance(val, int):
                            continue
                        if key == "id" or key in ROLE_FIELDS:
                            refs.append({"id": val, "section": section, "field": key})
    return refs


# --------------------------------------------------------------------------- #
# Snippet generation (copy-paste YAML for devices missing from the config)    #
# --------------------------------------------------------------------------- #
# Friendly emit order; sensors/binary_sensors last as they're usually bulk.
SECTION_ORDER = ["lights", "switches", "covers", "climates",
                 "multivalue_switches", "buttons", "fans", "valves",
                 "binary_sensors", "sensors", "times"]


def detect_item_indent(config_text):
    """Find the indentation your config uses for '- id:' list items, so the
    snippet pastes straight in. Returns (item_indent, section_indent)."""
    indents = [len(m.group(1))
               for m in re.finditer(r"(?m)^([ ]*)-\s", config_text)]
    if not indents:
        return 8, 6                       # martindybal default (under cores[])
    item = max(set(indents), key=indents.count)   # most common
    return item, max(item - 2, 0)


def build_snippet(not_set_up, config_text):
    """Return YAML text grouping every not-set-up device under its suggested
    section, indented to match the existing config."""
    item_i, sec_i = detect_item_indent(config_text)
    item_pad, sec_pad = " " * item_i, " " * sec_i

    grouped = {}
    for d in not_set_up:
        grouped.setdefault(suggest_section(d["S"]), []).append(d)

    lines = [
        f"{sec_pad}# ----------------------------------------------------------",
        f"{sec_pad}# TapHome devices exposed by the Core but NOT in your config.",
        f"{sec_pad}# Generated by taphome_probe.py — paste the lines you want",
        f"{sec_pad}# under the matching section of the right core, then tidy.",
        f"{sec_pad}# ----------------------------------------------------------",
    ]
    for sec in SECTION_ORDER:
        devs = grouped.get(sec)
        if not devs:
            continue
        lines.append("")
        lines.append(f"{sec_pad}{sec}:")
        for d in sorted(devs, key=lambda x: x["id"] or 0):
            note = d["name"] + (f" — {d['zone']}" if d["zone"] else "")
            lines.append(f"{item_pad}- id: {d['id']}  # {note}")
    return "\n".join(lines) + "\n"


EXAMPLE_API_URL = "http://<YOUR_CORE_IP>/api/TapHomeApi/v1"

_CONFIG_HEADER = """\
# =============================================================================
# TapHome configuration for the martindybal/taphome-homeassistant integration.
# Generated by taphome_probe.py — every device the Core exposes is listed below.
#
# 1) REPLACE THE PLACEHOLDERS in the core block:
#      id:          a unique name for this Core (e.g. "primary"). It prefixes
#                   entity ids and must be unique if you run multiple Cores.
#      token:       !secret taphome_API_key  -> add the real token to Home
#                   Assistant's secrets.yaml as:
#                       taphome_API_key: "your-real-token-here"
#                   Keep the !secret reference here; never commit the raw token.
#      api_url:     replace the IP with your Core's LAN address
#                   (http://<YOUR_CORE_IP>/api/TapHomeApi/v1).
#      webhook_id:  any unique id; register the matching webhook in TapHome so
#                   Home Assistant gets instant push updates.
#
# 2) YOU STILL NEED TO TUNE THE ENTITIES BELOW:
#      - sensors:        uncomment/adjust `device_class`, `unit_of_measurement`
#                        and `state_class` (suggestions are pre-filled where the
#                        value type is known; verify them).
#      - binary_sensors: uncomment/adjust `device_class`
#                        (door / window / motion / smoke / moisture / ...).
#      - Delete any device you do not want exposed, and move on/off devices
#        between `switches:` and `lights:` as you prefer.
#
# Reference: https://github.com/martindybal/taphome-homeassistant/blob/production/configuration.md
# =============================================================================
"""


def build_new_config(devices, api_url=None):
    """Generate a complete, ready-to-edit martindybal config from discovery."""
    url = api_url or EXAMPLE_API_URL
    grouped = {}
    for d in devices:
        grouped.setdefault(suggest_section(d["S"]), []).append(d)

    L = [_CONFIG_HEADER.rstrip("\n"), "", "cores:",
         "  - id: taphome                     # placeholder: name this Core",
         "    token: !secret taphome_API_key  # put the real token in secrets.yaml",
         f"    api_url: {url}"
         f"{' ' * max(1, 18 - len(url))}# replace IP with your Core's LAN address",
         "    webhook_id: taphome             # placeholder: any unique id"]

    for sec in SECTION_ORDER:
        devs = grouped.get(sec)
        if not devs:
            continue
        L.append("")
        if sec == "sensors":
            L.append("    # Sensors: uncomment device_class / unit / state_class as needed.")
        elif sec == "binary_sensors":
            L.append("    # Binary sensors: set a device_class for each.")
        L.append(f"    {sec}:")
        for d in sorted(devs, key=lambda x: x["id"] or 0):
            note = d["name"] + (f" — {d['zone']}" if d["zone"] else "")
            L.append(f"      - id: {d['id']}  # {note}")
            if sec == "sensors":
                hint = sensor_hint(d["S"])
                if hint:
                    dc, unit, sc = hint
                    L.append(f"        # device_class: {dc}" if dc
                             else "        # device_class:            # (none — set if needed)")
                    L.append(f'        # unit_of_measurement: "{unit}"')
                    L.append(f"        # state_class: {sc}")
                else:
                    L.append("        # device_class:                # e.g. temperature, power")
                    L.append('        # unit_of_measurement: ""      # e.g. "°C", "kWh"')
                    L.append("        # state_class: measurement     # or total_increasing")
            elif sec == "binary_sensors":
                dc = binary_hint(d["S"])
                L.append(f"        # device_class: {dc or '...'}"
                         "          # door/window/motion/smoke/moisture/...")
    return "\n".join(L) + "\n"


def cmd_init(devices, out_path, api_url=None, force=False):
    if os.path.exists(out_path) and not force:
        sys.exit(f"ERROR: {out_path} already exists. Use --force to overwrite, "
                 f"or --out to choose another path.")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(build_new_config(devices, api_url))
    print(f"Wrote a new martindybal config with {len(devices)} device(s) to:\n  {out_path}")
    print("Next: set the token in secrets.yaml, fix api_url/webhook_id, then tune "
          "sensor/binary_sensor classes.")


# --------------------------------------------------------------------------- #
# Reporting                                                                   #
# --------------------------------------------------------------------------- #
def vtypes_str(S):
    known = sorted(t for t in S if t in V)
    return ", ".join(f"{V[t]}" for t in known) or "(read-only/unknown)"


def cmd_probe(devices, as_json=None, raw=None):
    if as_json is not None:
        with open(as_json, "w", encoding="utf-8") as f:
            json.dump(raw, f, indent=2, ensure_ascii=False)
        print(f"Wrote raw discovery to {as_json}")
    print(f"\n{'ID':>5}  {'SECTION':<20} {'ZONE':<16} NAME")
    print("-" * 78)
    for d in sorted(devices, key=lambda x: (suggest_section(x["S"]), x["id"] or 0)):
        print(f"{str(d['id']):>5}  {suggest_section(d['S']):<20} "
              f"{(d['zone'] or '')[:15]:<16} {d['name']}")
        print(f"       └ {vtypes_str(d['S'])}")
    print(f"\n{len(devices)} devices exposed by the Core.")


def cmd_compare(devices, refs, snippet_path=None, config_text=""):
    by_id = {d["id"]: d for d in devices}
    referenced = {r["id"] for r in refs}

    misplaced, stale = [], []
    for r in refs:
        dev = by_id.get(r["id"])
        if dev is None:
            stale.append(r)
            continue
        S = dev["S"]
        field = r["field"]
        if field == "id":
            pred, desc = SECTION_PRIMARY.get(r["section"], (cap_any, "any device"))
        else:
            pred, desc = ROLE_FIELDS[field]
        if not pred(S):
            misplaced.append((r, dev, desc))

    not_set_up = [d for d in devices if d["id"] not in referenced]

    # ---- MISPLACED -------------------------------------------------------- #
    print("\n" + "=" * 70)
    print(f"  MISPLACED  ({len(misplaced)})  — listed in an incompatible section")
    print("=" * 70)
    if not misplaced:
        print("  none ✓")
    for r, dev, desc in sorted(misplaced, key=lambda x: x[0]["section"]):
        loc = r["section"] if r["field"] == "id" else f"{r['section']}.{r['field']}"
        print(f"  #{dev['id']:<5} {dev['name']}")
        print(f"        in '{loc}' but expects {desc}")
        print(f"        actually: {vtypes_str(dev['S'])}")
        print(f"        fits: {', '.join(valid_sections(dev['S']))}")

    # ---- NOT SET UP ------------------------------------------------------- #
    actionable = [d for d in not_set_up if not is_readonly(d["S"])]
    readonly = [d for d in not_set_up if is_readonly(d["S"])]
    print("\n" + "=" * 70)
    print(f"  NOT SET UP  ({len(not_set_up)})  — exposed by Core, absent from config")
    print("=" * 70)
    print(f"\n  Controllable ({len(actionable)}):")
    if not actionable:
        print("    none ✓")
    for d in sorted(actionable, key=lambda x: suggest_section(x["S"])):
        print(f"    #{d['id']:<5} [{suggest_section(d['S']):<19}] {d['name']}  "
              f"({d['zone']})")
        print(f"          {vtypes_str(d['S'])}")
    if readonly:
        print(f"\n  Read-only sensors ({len(readonly)}):")
        for d in sorted(readonly, key=lambda x: suggest_section(x["S"])):
            print(f"    #{d['id']:<5} [{suggest_section(d['S']):<19}] {d['name']}  "
                  f"({d['zone']})")

    # ---- STALE ------------------------------------------------------------ #
    print("\n" + "=" * 70)
    print(f"  STALE  ({len(stale)})  — in config but not reported by the Core")
    print("=" * 70)
    if not stale:
        print("  none ✓")
    for r in stale:
        loc = r["section"] if r["field"] == "id" else f"{r['section']}.{r['field']}"
        print(f"  #{r['id']:<5} referenced in '{loc}'")

    print("\n" + "-" * 70)
    print(f"summary: {len(misplaced)} misplaced, {len(actionable)} controllable "
          f"not set up, {len(readonly)} sensors not set up, {len(stale)} stale.")

    # ---- SNIPPET ---------------------------------------------------------- #
    if snippet_path:
        if not_set_up:
            with open(snippet_path, "w", encoding="utf-8") as f:
                f.write(build_snippet(not_set_up, config_text))
            print(f"\nWrote copy-paste snippet for {len(not_set_up)} new device(s) "
                  f"to:\n  {snippet_path}")
        else:
            print("\nNo new devices — snippet not written.")


# --------------------------------------------------------------------------- #
# CLI                                                                         #
# --------------------------------------------------------------------------- #
def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_source(sp):
        sp.add_argument("--url", help="API base, e.g. http://192.168.1.3/api/TapHomeApi/v1")
        sp.add_argument("--token", help="TapHome local API token")
        sp.add_argument("--discovery", help="read discovery from a JSON file instead of the Core")
        sp.add_argument("--timeout", type=int, default=10)

    sp = sub.add_parser("probe", help="list devices exposed by the Core")
    add_source(sp)
    sp.add_argument("--json", help="also dump raw discovery JSON to this file")

    sl = sub.add_parser("compare", help="compare Core devices against a martindybal config")
    add_source(sl)
    sl.add_argument("--config", required=True, help="path to configuration.yaml")
    sl.add_argument("--snippet", action="store_true",
                    help="write a copy-paste YAML snippet of NOT-SET-UP devices "
                         "next to the config file")
    sl.add_argument("--snippet-path",
                    help="override where the snippet is written")

    si = sub.add_parser("init", help="generate a brand-new martindybal config from "
                                     "the Core (when you have no config yet)")
    add_source(si)
    si.add_argument("--out", default="taphome.yaml",
                    help="output path for the new config (default: ./taphome.yaml)")
    si.add_argument("--force", action="store_true", help="overwrite --out if it exists")

    args = p.parse_args()

    if args.discovery:
        with open(args.discovery, "r", encoding="utf-8") as f:
            raw = json.load(f)
    elif args.url and args.token:
        raw = fetch_discovery(args.url, args.token, args.timeout)
    else:
        sys.exit("ERROR: provide --url and --token, or --discovery <file>.")

    devices = devices_from_discovery(raw)

    if args.cmd == "probe":
        cmd_probe(devices, as_json=args.json, raw=raw)
    elif args.cmd == "init":
        cmd_init(devices, args.out, api_url=args.url, force=args.force)
    else:
        refs = load_config_refs(args.config)
        with open(args.config, "r", encoding="utf-8") as f:
            config_text = f.read()
        snippet_path = None
        if args.snippet or args.snippet_path:
            snippet_path = args.snippet_path or os.path.join(
                os.path.dirname(os.path.abspath(args.config)),
                os.path.splitext(os.path.basename(args.config))[0] + ".new-devices.yaml",
            )
        cmd_compare(devices, refs, snippet_path=snippet_path, config_text=config_text)


if __name__ == "__main__":
    main()
