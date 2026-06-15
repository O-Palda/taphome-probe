# TapHome Probe & Config Constructor

A tiny command-line tool for [TapHome](https://www.taphome.com/) smart-home owners
who use Home Assistant and martindybal's integration [martindybal/taphome-homeassistant](https://github.com/martindybal/taphome-homeassistant)
. It talks to your TapHome **Core's local API** and helps you
keep your integration configuration in sync with the devices the Core actually
exposes.

It does a few things:

- **`probe`** — list every device the Core exposes, with the detected device type
  and the Home Assistant section it belongs in.
- **`init`** — when you have **no config yet**, generate a complete, commented
  [martindybal/taphome-homeassistant](https://github.com/martindybal/taphome-homeassistant)
  config from the Core, with placeholders to fill in.
- **`compare`** — compare the Core's devices against an existing `configuration.yaml`
  and flag what's **misplaced**, **not set up**, or **stale**.
- **`compare --snippet`** — generate a ready-to-paste YAML block for the devices that
  are missing from your config, indented to match your existing file.

> Not affiliated with or endorsed by TapHome. This is an independent, community tool.

---

## Why

The [martindybal](https://github.com/martindybal/taphome-homeassistant) integration is
configured entirely in YAML: every device is listed by ID under a section
(`lights`, `switches`, `climates`, …). When you add or change devices in TapHome it's
easy to (a) forget to add the new device, or (b) put it in the wrong section. This tool
diffs your YAML against the Core's live device list and tells you exactly what's off —
and writes you the YAML to fix it.

The device-classification rules are derived from the same `/discovery` value-type
metadata that the Home Assistant integrations themselves use, so the verdicts match
what those integrations do at runtime.

---

## Requirements

- Python 3.9+
- [`PyYAML`](https://pypi.org/project/PyYAML/) — **only** for `compare`
  (`probe` is standard-library only)

```bash
pip install -r requirements.txt   # or: pip install pyyaml
```

## Getting your API URL and token

In the TapHome app: **Settings → Expose Devices**. Enable the local API and copy the
**token**. The API base URL is:

```
http://<YOUR_CORE_IP>/api/TapHomeApi/v1
```

This is the official **TapHome API**, documented by TapHome at
<https://taphome.com/en/docs/expose-devices/taphome-api/>. The same API is also
reachable via TapHome's cloud at `https://api.taphome.com/api/TapHomeApi/v1/`. This
tool only ever **reads** the `discovery` endpoint (and writes nothing).

---

## Usage

### Probe — see what the Core exposes

```bash
python3 taphome_probe.py probe \
  --url http://192.168.1.3/api/TapHomeApi/v1 \
  --token YOUR_TOKEN
```

```
   ID  SECTION              ZONE             NAME
------------------------------------------------------------------------------
    1  lights               Kitchen          Kitchen Light
       └ AnalogOutput, SwitchState, AnalogDesired
    2  covers               Living           Living Blind
       └ BlindsSlope(tilt), BlindsLevel, BlindsIsMoving
    3  climates             Bath             Bath Thermostat
       └ Temperature, TargetTemperature
...
```

### Init — scaffold a brand-new config (no config yet)

If you're starting from scratch, generate a complete martindybal config straight from
the Core:

```bash
python3 taphome_probe.py init \
  --url http://192.168.1.3/api/TapHomeApi/v1 \
  --token YOUR_TOKEN \
  --out taphome.yaml
```

It writes `taphome.yaml` (override with `--out`; refuses to overwrite unless
`--force`) containing a ready-to-edit `cores:` block and every device grouped under
its section. A header comment explains exactly which placeholders to replace
(`id`, `token` via `!secret`, `api_url`, `webhook_id`) and reminds you to set
`device_class` / `unit_of_measurement` / `state_class` on sensors and `device_class`
on binary sensors — with suggested values pre-filled (commented out) where the value
type is recognised:

```yaml
cores:
  - id: taphome                     # placeholder: name this Core
    token: !secret taphome_API_key  # put the real token in secrets.yaml
    api_url: http://192.168.1.3/api/TapHomeApi/v1
    webhook_id: taphome             # placeholder: any unique id

    lights:
      - id: 1  # Kitchen Light — Kitchen

    sensors:
      - id: 7  # Outside Temp — Roof
        # device_class: temperature
        # unit_of_measurement: "°C"
        # state_class: measurement
```

Then store the real token in Home Assistant's `secrets.yaml`
(`taphome_API_key: "…"`), adjust the placeholders, and trim/tune the entities.

### Compare — diff against your martindybal config

```bash
python3 taphome_probe.py compare \
  --url http://192.168.1.3/api/TapHomeApi/v1 \
  --token YOUR_TOKEN \
  --config /config/configuration.yaml
```

Reports three buckets:

| Bucket          | Meaning                                                                 |
|-----------------|-------------------------------------------------------------------------|
| **Misplaced**   | An ID listed in a section incompatible with its capabilities (e.g. a multi-value switch under `lights:`, a non-thermostat under `climates:`, an `hvac_switch_id` that isn't a switch). |
| **Not set up**  | Devices the Core exposes that aren't referenced anywhere in your YAML — split into *controllable* and *read-only sensors*. |
| **Stale**       | IDs in your YAML that the Core no longer reports.                        |

Home Assistant custom YAML tags (`!secret`, `!include`, …) are ignored, so the tool
runs against your real `configuration.yaml` without choking. Pass the **actual** token
on the command line (the `!secret` in your YAML is intentionally skipped).

### Generate a copy-paste snippet for new devices

```bash
python3 taphome_probe.py compare \
  --url http://192.168.1.3/api/TapHomeApi/v1 \
  --token YOUR_TOKEN \
  --config /config/configuration.yaml \
  --snippet
```

Writes `<config-name>.new-devices.yaml` next to your config (override with
`--snippet-path`). It groups the missing devices under their suggested sections and
matches your file's indentation, so you can paste it straight into the right core:

```yaml
    lights:
      - id: 9001  # New Kitchen Dimmer — Kitchen

    covers:
      - id: 9002  # New Blind — Living

    climates:
      - id: 9003  # New Thermostat — Bath
```

### Offline workflow (probe on the HA host, compare anywhere)

```bash
# On the host that can reach the Core:
python3 taphome_probe.py probe --url http://192.168.1.3/api/TapHomeApi/v1 \
  --token YOUR_TOKEN --json discovery.json

# Anywhere (no token needed):
python3 taphome_probe.py compare --discovery discovery.json --config configuration.yaml --snippet
```

---

## How classification works

Each TapHome device reports a set of *value types* (e.g. `SwitchState`, `BlindsLevel`,
`TargetTemperature`). The tool maps those to a Home Assistant section using the same
priority order the integrations use — for example:

- `BlindsLevel` + `BlindsIsMoving` → **cover**
- `TargetTemperature` → **climate**
- `AnalogOutput` / `HueBrightness` / `ColorTemp` → **light**
- `MultiValueSwitch` → **select (multivalue_switches)**
- `SwitchState` → **switch**
- `Button` → **button**
- contact/motion/flood/smoke value types → **binary_sensor**
- measurement value types → **sensor**

"Misplaced" is deliberately high-precision: it only flags genuine capability
mismatches (the kind the integration would reject at load), not stylistic choices —
e.g. a plain on/off device may legitimately be exposed as either a `switch` or a
`light`, so that's never flagged.

---

## Acknowledgements

### TapHome

This tool is built entirely on the official **[TapHome API](https://taphome.com/en/docs/expose-devices/taphome-api/)**
provided by **[TapHome](https://www.taphome.com/)**. All device data, the
`discovery` endpoint, the authentication scheme (`Authorization: TapHome {token}`),
and the `valueTypeId` / `valueTypeName` identifiers used throughout this tool come
from TapHome's API and its documentation. Full credit and thanks to TapHome for
exposing a clean, documented local API.

"TapHome" is a trademark of its respective owner. This project is an independent,
unofficial community tool and is **not affiliated with, endorsed by, or sponsored by
TapHome**.

### Home Assistant integrations

The device-classification logic was inspired by, and studied from, two excellent
open-source TapHome integrations for Home Assistant:

- **[martindybal/taphome-homeassistant](https://github.com/martindybal/taphome-homeassistant)**
  — the YAML-configured integration this linter targets. Its
  `taphome_sdk/device_factory.py` and value-type enum were the reference for the
  device-classification order and the TapHome value-type IDs.
- **[khral07/ha-taphome-lokal](https://github.com/khral07/ha-taphome-lokal)**
  — a GUI auto-discovery integration; its per-platform discovery filters were used to
  cross-check the classification rules and the local API endpoints.

No source code from either project is included in this repository; the logic was
reimplemented independently. All credit for the underlying integrations goes to their
respective authors. The `valueTypeId` identifiers are defined by the
[official TapHome API](https://taphome.com/en/docs/expose-devices/taphome-api/).

---

## License

[GPL-3.0-or-later](LICENSE) — matching the source integrations that inspired it.

Copyright (C) 2026 `O-Palda`.

---

## Contributing

Issues and pull requests welcome. This is a single-file script with no build step —
clone, edit `taphome_probe.py`, run. If you hit a device the classifier gets wrong,
please open an issue with the device's value-type list (from `probe`) so the rules can
be improved.
