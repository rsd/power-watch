# power-watch

power-watch watches how heavily a laptop loads its power adapter and alerts
before a sustained overload. `power-cap` lowers CPU and GPU power limits as a
stopgap when an adapter is undersized or running hot. Both tools are small,
stdlib-only Python scripts; neither ever writes to the embedded controller (EC).

- `power-watch`: live view, one-shot JSON, headless alerting and history
  (systemd user unit).
- `power-cap`: show, set and restore CPU (RAPL) and NVIDIA GPU power limits.

An optional Omarchy bar widget, [omarchy-power-watch](https://github.com/rsd/omarchy-power-watch), lives in its own
repository. It shows the load and its history from the files the service writes
(see [Data contract](#data-contract-public-interface)).

## Why the number is measured or estimated

On most laptops there is no sysfs sensor for the power entering from the
adapter. The EC knows it but does not expose it. power-watch therefore uses the
best source available:

| Situation | Total | Kind |
|---|---|---|
| On battery | battery discharge power, summed over all batteries | measured |
| On AC, RAPL `psys` domain present | platform power from `psys` | measured |
| On AC, otherwise | CPU package (RAPL) + discrete GPUs + rest of system + battery charge power | estimated |

Notes:

- **`psys`.** Whether it includes battery charging power is platform-dependent.
  `--no-psys` (or `psys = off` in the config) ignores it and always estimates.
- **Discrete GPUs.** NVIDIA via `nvidia-smi` `power.draw`; AMD via hwmon
  `power1_average`, else `power1_input`. Integrated GPUs (Intel, AMD APU) are
  inside the CPU package and are not counted twice. An AMD GPU is treated as an
  APU, and skipped, when its hwmon directory has an `in1_input` sensor (a
  heuristic).
- **Rest of system** (screen, board, disks, fans) is set by calibration on
  battery (`power-watch calibrate`). Until then it is 25 W, labelled `DEFAULT`
  in the output.
- **Charge power** is added only while the battery status is `Charging`.
- **Unknown terms.** A term that cannot be read counts as 0, and the total is
  then a labelled lower bound ("≥"). Alerts still fire on a lower bound.
- **GPU runtime power management.** A runtime-suspended discrete GPU is never
  queried, because that would wake it. It is reported as `suspended` at 0 W.
  Limitation: polling an *active* NVIDIA GPU every interval may keep it from
  suspending on hybrid laptops. Use `--no-gpu` (or `[gpu] enabled = no`) or a
  longer `--interval` if that matters.

Treat the estimate as roughly +/-10-15 %: rest-of-system drifts with brightness,
fans and USB devices; battery-side charge power ignores charger losses; RAPL
excludes voltage-regulator losses. Keep margin under your threshold.

## Install

Requirements:

- Linux with systemd, Python 3.10 or newer, standard library only.
- Optional: `nvidia-smi` (NVIDIA GPU power), `notify-send` (alerts), Omarchy
  (the bar widget).

Tested on Arch Linux with Omarchy only. Other systemd distributions are
expected to work but are untested.

```
sudo ./install.sh [--group NAME]
./install.sh --user
```

`sudo ./install.sh` installs, under `PREFIX` (default `/usr/local`; `DESTDIR` is
supported for staging):

- `$PREFIX/bin/power-watch` and `$PREFIX/bin/power-cap`
- `$PREFIX/lib/systemd/user/power-watch.service`
- `$PREFIX/share/power-watch/config.example`
- `/etc/tmpfiles.d/power-watch.conf`

It applies the tmpfiles rule at once with `systemd-tmpfiles --create`. The rule
makes the RAPL `energy_uj` counters readable (mode `0440`) by one group, so
power-watch can read CPU power without root. The group is `--group NAME`, else
`$RAPL_GROUP`, else `wheel` if it exists, else `sudo`. If neither exists, the
install stops and asks for `--group`. It never creates or overwrites a user
config.

**Security trade-off.** Since CVE-2020-8694 ("Platypus") the kernel keeps
`energy_uj` root-only, because fine-grained energy readings are a power side
channel that can leak secrets from other processes. The rule re-opens it to the
chosen group. That is reasonable when the group already means "can become root"
(`wheel`, or `sudo` with sudo rights). Otherwise pass a narrower `--group`, for
example a dedicated group holding only the users who run power-watch.

**After a RAPL module reload.** If `intel_rapl` loads after
`systemd-tmpfiles-setup`, or is reloaded, the files revert to `0400 root`.
power-watch then says so (and `--quiet` notifies). Re-apply with:

```
sudo systemd-tmpfiles --create /etc/tmpfiles.d/power-watch.conf
```

`./install.sh --user` is a separate step, run as your normal user (it refuses
root), because `systemctl --user` needs your session bus. It:

- runs `systemctl --user daemon-reload`, enables the service and restarts it;
- offers the [Omarchy bar widget](#omarchy-bar-widget) when `omarchy` is on
  `PATH` and the widget is not installed yet. In a terminal it runs `omarchy
  plugin add https://github.com/rsd/omarchy-power-watch --enable`, which asks you to confirm the
  plugin; otherwise it prints that command, as it does when you decline or the
  add fails (not an error). An installed widget is left alone: one in
  `~/.config/omarchy/plugins/io.github.rsd.power-watch`, or one `omarchy plugin
  list` reports from another plugin dir. `POWER_WATCH_INTERACTIVE=0` skips the
  offer, `=1` makes it without a terminal check;
- removes the `~/.config/omarchy/plugins/io.github.rsd.power-watch` link into
  this checkout's `omarchy-plugin/` that older versions created (by either path
  to the checkout, or any dangling link to an `.../omarchy-plugin`), since the
  widget moved to its own repository. It tries `omarchy plugin disable` first
  (which usually fails once the target is gone or no shell runs: reported, not
  an error) and afterwards `omarchy-shell shell rescanPlugins`, so a running
  shell drops the entry.

Each failed service step prints a warning with the command to retry, and the
exit code is non-zero. Without Omarchy you still get the service. To pass one-off flags
to the service, run `systemctl --user edit power-watch` and override
`ExecStart`.

### Uninstall

```
./install.sh --user --uninstall    # as your user: disable the service
sudo ./install.sh --uninstall      # remove the installed files
```

The user uninstall leaves the bar widget alone (it prints `omarchy plugin
remove io.github.rsd.power-watch` when the widget is installed), except for the
old link into `omarchy-plugin/`, which it removes as `--user` does.

The system uninstall removes the binaries, the unit, the tmpfiles rule and the
example config. It leaves your config alone, and the `energy_uj` read access
stays until reboot or a RAPL module reload. Optionally delete your data:

```
rm -r ~/.config/power-watch ~/.local/state/power-watch
```

(`$POWER_WATCH_STATE_DIR` or `$XDG_STATE_HOME/power-watch` if you set either.)
A `PREFIX` containing a space or `%` breaks the installed unit's `ExecStart`.

## Configuration

Everything is optional. Without a config, power-watch works with built-in
defaults, but it cannot show amps or a percentage of the rating until it knows
your adapter.

Copy the example and edit it:

```
mkdir -p ~/.config/power-watch
cp /usr/local/share/power-watch/config.example ~/.config/power-watch/config
```

Which file is read, first hit wins:

1. `--config PATH` (must exist)
2. `$POWER_WATCH_CONFIG` (must exist)
3. `power-cap` run as root via `sudo` or `doas`: the invoking user's
   `~/.config/power-watch/config`, read with that user's permissions
4. `$XDG_CONFIG_HOME/power-watch/config` (a relative `XDG_CONFIG_HOME` is
   ignored), else `~/.config/power-watch/config`; a missing file means defaults

Precedence for each setting: command-line flag > config file > built-in default.
Restart the service after editing the file.

| Section | Key | Default | Meaning |
|---|---|---|---|
| `[adapter]` | `voltage_v` | unset | Adapter output voltage; unset means no amps shown |
| `[adapter]` | `rating_w` | unset | Adapter rating; unset means no % of rating shown |
| `[alert]` | `threshold_w` | 80 % of `rating_w`, else 150 | Alert above this load |
| `[alert]` | `sustain_s` | 10 | Seconds the load must stay above the threshold |
| `[alert]` | `every_s` | 300 | Seconds between repeated alerts |
| `[estimate]` | `rest_w` | 25 | Rest-of-system W before calibration |
| `[estimate]` | `psys` | `auto` | `auto` or `off`: use a RAPL `psys` domain |
| `[gpu]` | `enabled` | `yes` | `no` is the same as `--no-gpu` |
| `[gpu]` | `devices` | empty (all discrete GPUs) | PCI addresses, comma or space separated |
| `[power-cap]` | `safe_cpu_w` | half the original CPU PL1, at least 5 | CPU cap for `power-cap set --safe` |
| `[power-cap]` | `safe_gpu_w` | half the original NVIDIA enforced limit, within the card's min/max | GPU cap for `--safe` |

`power-watch` reads `[adapter]`, `[alert]`, `[estimate]` and `[gpu]`;
`power-cap` reads only `[power-cap]`. An invalid value is an error (exit 2)
that names the file, section and key. `sudo power-cap set --safe` uses the
`[power-cap]` values from the invoking user's file. The example file ends with
a labelled illustration for a gaming laptop with a 330 W, 19.5 V adapter.

## Usage

```
power-watch                     # live view
power-watch --once              # one measurement, then exit (--window S, default 1)
power-watch --once --json       # one JSON object
power-watch --quiet             # headless: sample, alert, log, record history (what the unit runs)
power-watch --log PATH          # also append one CSV row per sample
power-watch calibrate [--duration 60] [--interval S]
power-watch history [--range 1h|24h|7d] [--json]
```

Other options: `--interval` (default 2 s), `--config`, `--adapter-voltage`,
`--adapter-rating`, `--threshold`, `--sustain`, `--alert-every`, `--rest`,
`--no-gpu`, `--gpu PCI` (repeatable), `--no-psys`. See `power-watch --help`.

**Calibration.** `power-watch calibrate` asks you to unplug the adapter,
samples about 60 s on battery, and saves the median rest-of-system power to
`~/.local/state/power-watch/calibration.json`. While on battery, the running
modes (live, `--quiet`) also calibrate passively from a rolling median, and the
latest on-battery data wins. Calibrate under typical load and brightness.

**Alerts.** When the load stays above the threshold for the sustain time on AC,
`notify-send -u critical` fires, and again every `every_s` while it persists.
Lower-bound alerts say so. With `--quiet`, an unreadable RAPL sends its own
notification.

### JSON output and history

`--once --json` prints one object with the keys of `current.json`, without its
four service-only keys. Only `power-watch --quiet` records `current.json` and
the history tiers. Keys, files and formats are in
[Data contract](#data-contract-public-interface). The CSV `--log` columns are a
frozen schema, unchanged by the newer JSON keys.

`power-watch history` prints Max, Average and Current; `--json` prints
`{range, tier, step_s, from, to, points, stats, threshold_w}`.

### power-cap

```
power-cap status
sudo power-cap set --cpu W [--cpu-pl2 W] --gpu W
sudo power-cap set --safe
sudo power-cap reset
```

- `--cpu` sets PL1 and, unless `--cpu-pl2` is given, PL2 as well. `--cpu-pl2`
  needs `--cpu`; `--safe` cannot be combined with the other flags.
- `--safe` uses the `[power-cap]` caps, by default half the original limits.
  `power-cap status` shows the resolved values.
- Originals are saved on the first `set` under `/run/power-cap` and restored by
  `reset`. They are lost at reboot, which also resets the hardware limits, so a
  cap never outlives a reboot.
- Every limit is read back after writing, and refusals are reported.
- A permission error as root is reported as a firmware lock.
- After a reset, `/run/power-cap` stays root-only, so unprivileged
  `power-cap status` says "Cap: unknown" until reboot. Use sudo.

## Data contract (public interface)

The state dir, `current.json` and the `history-*.jsonl` tiers that `power-watch
--quiet` (the user service) writes are power-watch's public interface. The
[omarchy-power-watch](https://github.com/rsd/omarchy-power-watch) bar widget reads them, and so may
any other consumer. Readers never write to the state dir.

### State dir

First hit wins:

1. `$POWER_WATCH_STATE_DIR`, as is
2. `$XDG_STATE_HOME/power-watch`
3. `~/.local/state/power-watch`

An empty variable counts as unset. Trailing slashes are dropped (`/` stays `/`).
Nothing is trimmed: a leading space is part of the path. A consumer resolves
the dir in its own environment (the widget in the Omarchy shell's), so set a
variable for both or for neither.

The dir holds `current.json`, `history-raw.jsonl`, `history-1m.jsonl` and
`history-15m.jsonl`. `history.lock` (the writer's `flock`) and
`calibration.json` are internal and not part of the contract.

### current.json

The latest sample: the `--once --json` report plus `interval_s`,
`cpu_limit_w`, `gpu_max_limit_w` and `epoch`. After every sample it is written
complete to a temporary file in the same dir and renamed over the old one
(`os.replace`), so a reader sees the old file or the new one, never a partial
one, and a directory watch sees every update. It is a few hundred bytes; the
widget does not read one over 16 KiB. A sample whose `epoch` is older than 3 x
`interval_s` means the service is not running.

Numbers are JSON numbers (never strings). Measured and estimated watts and
amps are rounded to 2 decimals and `battery_capacity` is a whole percent;
`epoch` is not rounded (fractional seconds), and `cpu_limit_w` and the
configured `adapter_v`, `adapter_rating_w` and `threshold_w` are written as
read or configured. `null` means unknown or not applicable, as listed.

| Key | Type | Meaning, unit | `null` when |
|---|---|---|---|
| `time` | string | local time of the sample, ISO 8601 with offset | never |
| `epoch` | number | Unix time of the sample, s | never |
| `interval_s` | number | sampling interval, s | never |
| `ac_online` | boolean | adapter plugged in | AC state unreadable |
| `battery_status` | string | kernel battery status (`Charging`, `Discharging`, `Not charging`, ...) | no battery or unreadable |
| `battery_capacity` | number | battery charge, % | unreadable |
| `battery_w` | number | battery power, W | unreadable |
| `cpu_w` | number | CPU package power (RAPL), W | RAPL unreadable |
| `gpu_w` | number | discrete GPUs summed, W (0 when none, disabled or suspended) | a GPU's power unreadable |
| `gpu_limit_w`, `gpu_max_limit_w` | number | NVIDIA enforced and maximum power limit, W | no active NVIDIA GPU read |
| `gpu_pstate` | string | NVIDIA P-state (`P0` ... `P12`) | no active NVIDIA GPU read |
| `gpu_status` | string | `active`, `suspended`, `none`, `disabled` or `unknown` | never |
| `gpu_devices` | array | one `{pci, vendor, status, power_w}` per discrete GPU: `pci` like `0000:01:00.0`, `vendor` `nvidia` or `amd`, `status` `active`, `suspended` or `unknown`, `power_w` W or `null` | never (empty list) |
| `rest_w` | number | rest of system, W | never |
| `rest_source` | string | `default`, `calibrate` or `passive` | never |
| `charge_w` | number | battery charge power counted on AC, W (0 when not charging or unreadable) | never |
| `total_w` | number | the adapter load, W | on battery with the discharge unreadable |
| `total_kind` | string | `measured` or `estimated` | never |
| `total_source` | string | `battery`, `psys` or `model` | never |
| `total_lower_bound` | boolean | an estimate with an unknown term counted as 0 | never |
| `incomplete` | boolean | a term the total depends on is unknown | never |
| `psys_w` | number | RAPL `psys` platform power, W | no `psys`, or `--no-psys` |
| `adapter_v`, `adapter_rating_w` | number | configured adapter voltage (V) and rating (W) | not configured |
| `adapter_current_a` | number | adapter current, A | voltage not configured, or on battery |
| `threshold_w` | number | resolved alert threshold, W | never |
| `cpu_limit_w` | number | CPU long-term limit (PL1), W | unreadable |
| `errors` | array | read errors of this sample, strings | never (empty list) |

The widget reads every key except `time`. It treats a value outside the listed
values or types as not reported.

### History tiers

| File | One line per | Kept |
|---|---|---|
| `history-raw.jsonl` | sample | 1 h |
| `history-1m.jsonl` | 60 s bucket | 48 h |
| `history-15m.jsonl` | 900 s bucket | 14 d |

Each line is one JSON object, ASCII and well under 1024 characters (the widget
skips a longer line); the widget reads all of these fields, the 1h,
24h and 7d ranges from the raw, 1m and 15m tier:

| Field | Type | Meaning |
|---|---|---|
| `t` | number | bucket start, Unix s (raw: the sample's `epoch` rounded to 2 decimals) |
| `n` | integer | samples in the bucket (raw: 1) |
| `avg`, `max` | number or `null` | mean and maximum `total_w`, W |
| `cpu`, `gpu`, `rest`, `chg` | number or `null` | mean `cpu_w`, `gpu_w`, `rest_w`, `charge_w`, W |
| `ac` | number or `null` | fraction of samples on AC, 0 to 1 |
| `k` | string | `m` all measured, `e` all estimated, `x` mixed |

Values are rounded to 2 decimals, and a mean is `null` when no sample in the
bucket had that value. Lines are appended in increasing `t`, a bucket's line
when the bucket closes; on a clean stop the open buckets are appended too. A
line is never written with a `t` at or before the last one. At start and when
a file grows past 1.25 x its retention, it is compacted: rewritten to a
temporary file, fsynced and renamed over the original, keeping the lines
inside the retention. A crash can leave a torn last line, so readers skip
lines that do not parse. Points more than 2.5 steps apart are a gap: readers
break the line and never interpolate. A `flock` on `history.lock` keeps a
second `--quiet` instance from writing.

### Stability

Additive changes are compatible: a new key in `current.json`, a new field in a
history line, a new file in the state dir, a longer retention. Consumers ignore
what they do not know. Renaming or removing a key, field or file, changing a
JSON type or an enumerated value, changing the state dir resolution, giving up
the atomic rename of `current.json`, or keeping a tier for less than the range
the widget shows from it, is a breaking change for the widget.
`tests/test_contract.py` pins all of this against the real service.

## Omarchy bar widget

The bar widget is a separate Omarchy shell plugin,
[rsd/omarchy-power-watch](https://github.com/rsd/omarchy-power-watch) (id `io.github.rsd.power-watch`). It
shows the load in the bar, coloured as it nears the threshold, and a panel
with the 1h, 24h and 7d history. Add it with:

```
omarchy plugin add https://github.com/rsd/omarchy-power-watch --enable
```

It needs the service running (`./install.sh --user`, which offers that command
when run in a terminal). It reads only the files in
[Data contract](#data-contract-public-interface). Update it with `omarchy
plugin update io.github.rsd.power-watch` and remove it with `omarchy plugin
remove io.github.rsd.power-watch`. Its documentation and tests live in its
repository.

## Limitations

- Adapter input power is not measured, except through `psys` where present.
- `psys` semantics (for example whether it includes battery charging) vary by
  platform.
- `power-cap` GPU capping works for NVIDIA only, on the first NVIDIA GPU
  (`nvidia-smi -i 0`). AMD and Intel GPU capping is not implemented.
- Intel Arc discrete GPUs are not measured.
- The AMD APU heuristic is untested on real hardware.
- Multiple NVIDIA GPUs and the `-i` subset logic are untested beyond one GPU.
- If the firmware locks the power limits, `set` is refused. `reset` may keep
  reporting a cap as active until reboot, although nothing was changed.
- Whether a mobile GPU accepts `nvidia-smi -pl`, and whether writing PL2 has an
  effect, depends on the hardware and firmware.

## Testing

```
python3 -m pytest -q tests/
```

Needs only the standard library and pytest. `tests/test_contract.py` guards
the [Data contract](#data-contract-public-interface) the bar widget reads. Fixtures are injected through
`POWER_WATCH_SYSFS`, `POWER_WATCH_NVIDIA_SMI`, `POWER_WATCH_STATE_DIR`,
`POWER_WATCH_NOTIFY` and `POWER_CAP_STATE_DIR`, so no root and no GPU are needed.

### Manual test plan

Not covered by the automated tests, which need no root and no real hardware.
Compare each expectation with your own machine's specs.

1. `sudo ./install.sh`. Expect the files installed and the tmpfiles rule applied.
   `ls -l /sys/class/powercap/intel-rapl:0/energy_uj` shows mode `-r--r-----`
   with your chosen group.
2. `power-watch --once --json`. Expect `cpu_w` non-null, `errors` empty, exit 0.
   Run `power-watch` live: idle CPU power is low, and under an all-core load it
   rises toward your CPU's long-term power limit. A discrete GPU reads its idle
   draw when active and `suspended` at 0 W when runtime-suspended.
3. `power-watch calibrate`. Unplug when told; after about 60 s a rest value is
   saved; replug. `power-watch --once` now shows rest source `calibrate`.
4. `sudo power-cap status`. Expect your CPU PL1 and GPU limit and "Cap: none".
5. `sudo power-cap set --safe`. Read the per-limit report and note which writes
   were accepted. Confirm with `sudo power-cap status` and `nvidia-smi -q -d
   POWER`. Under load, CPU power should settle at or below the cap.
6. Alert test: `power-watch --threshold 50 --sustain 4` under load, on AC. Expect
   one critical notification, not repeated within `--alert-every`.
7. `sudo power-cap reset`. Expect your original limits back and "Cap: none".
8. `./install.sh --user` in a terminal. Expect `systemctl --user status
   power-watch` active, no warnings, and with Omarchy the offer to add the bar
   widget (omarchy asks you to confirm). Run it again: the widget is reported
   as already installed and nothing is added.
   - Reboot and check whether `energy_uj` kept its mode. If not, re-apply the
     tmpfiles rule (see Install).
   - `ls ~/.local/state/power-watch`: `current.json` and the three
     `history-*.jsonl` files. `power-watch history --json` shows points.
   - `./install.sh --user --uninstall`: the service stops; the widget stays,
     with the `omarchy plugin remove` hint. The widget's own checks live in its
     repository.

## License

MIT. See `LICENSE`.
