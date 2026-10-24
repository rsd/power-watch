# power-watch

power-watch watches how heavily a laptop loads its power adapter and alerts
before a sustained overload. `power-cap` lowers CPU and GPU power limits as a
stopgap when an adapter is undersized or running hot. An optional Omarchy bar
widget shows the load and its history. Both tools are small, stdlib-only Python
scripts; neither ever writes to the embedded controller (EC).

- `power-watch`: live view, one-shot JSON, headless alerting and history
  (systemd user unit).
- `power-cap`: show, set and restore CPU (RAPL) and NVIDIA GPU power limits.
- `omarchy-plugin/`: the optional Omarchy bar widget.

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
- Optional: `nvidia-smi` (NVIDIA GPU power), `notify-send` (alerts), `node` and
  `qmllint` (some tests).

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

- symlinks `omarchy-plugin/` as `~/.config/omarchy/plugins/io.github.rsd.power-watch`
  (a real directory at that path is left alone, with an error);
- runs `systemctl --user daemon-reload`, enables the service and restarts it;
- rescans the Omarchy shell's plugins and enables the plugin, when `omarchy` is
  on `PATH`.

Each failed step prints a warning with the command to retry, and the exit code
is non-zero. Without Omarchy you still get the service. To pass one-off flags
to the service, run `systemctl --user edit power-watch` and override
`ExecStart`.

### Uninstall

```
./install.sh --user --uninstall    # as your user: disable plugin and service, remove the link
sudo ./install.sh --uninstall      # remove the installed files
```

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

### JSON fields

`--once --json` and `current.json` carry these keys. Unknown values are `null`.

| Key | Meaning |
|---|---|
| `time`, `ac_online`, `battery_status`, `battery_capacity`, `battery_w` | Time, AC state and battery state |
| `cpu_w`, `gpu_w`, `rest_w`, `rest_source`, `charge_w` | Terms of the estimate |
| `gpu_limit_w`, `gpu_pstate` | GPU power limit and P-state (NVIDIA) |
| `total_w`, `total_kind` | Total, `measured` or `estimated` |
| `total_source` | `battery`, `psys` or `model` |
| `total_lower_bound` | `true` when an estimate has unknown terms (counted as 0) |
| `psys_w` | RAPL `psys` platform power, or `null` |
| `gpu_status` | `active`, `suspended`, `none`, `disabled` or `unknown` |
| `gpu_devices` | List of `{pci, vendor, status, power_w}` |
| `incomplete` | Some term the total depends on is unknown |
| `adapter_v`, `adapter_current_a`, `adapter_rating_w` | `null` when voltage or rating is unknown |
| `threshold_w`, `errors` | Resolved threshold and read errors |

`current.json` adds `interval_s`, `cpu_limit_w`, `gpu_max_limit_w` and `epoch`.
The CSV `--log` columns are a frozen schema, unchanged by the newer JSON keys.

### History

Only `power-watch --quiet` records history. Files live in the state dir
(`$POWER_WATCH_STATE_DIR`, else `$XDG_STATE_HOME/power-watch`, else
`~/.local/state/power-watch`):

| File | Holds | Kept |
|---|---|---|
| `history-raw.jsonl` | every sample | 1 h |
| `history-1m.jsonl` | 60 s buckets | 48 h |
| `history-15m.jsonl` | 900 s buckets | 14 d |
| `current.json` | the latest sample, rewritten atomically | latest only |

Each history line holds `t` (bucket start, Unix seconds), `n` (samples), `avg`
and `max` of the total, mean `cpu`, `gpu`, `rest`, `chg`, `ac` (fraction of
samples on AC) and `k` (`m` measured, `e` estimated, `x` mixed). Points more
than 2.5 steps apart are a gap; readers break the line and never interpolate.
Files are size-bounded and compacted automatically, and a `flock` keeps a
second `--quiet` instance from writing. `power-watch history` prints Max,
Average and Current; `--json` prints `{range, tier, step_s, from, to, points,
stats, threshold_w}`.

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

## Omarchy bar widget

`omarchy-plugin/` is an Omarchy shell plugin, id `io.github.rsd.power-watch`
(MIT, with its own `LICENSE`). Install it with `./install.sh --user`; it needs
the service running.

- **Bar.** Shows the load in watts, coloured as normal, approaching (from 85 % of
  the threshold) or over. A lower bound is marked "≥". Measured platform power
  and a suspended GPU are labelled. The adapter amps and share of rating show
  "Not configured" when voltage or rating is not set in the config.
- **Panel** (click the widget). Charts for 1h, 24h and 7d: average, max, CPU and
  GPU, the threshold, and a band for the source (battery measured, AC measured
  or estimated, mixed). Below that: Max, Average, Current, the current breakdown
  and the power caps. Keys: left/right change range, Esc closes.
- **Staleness.** The number goes stale, and the widget is dimmed with a hint,
  after 3 x max(`interval_s`, 1 s) without a new sample.
- **What it reads.** Only `current.json` and the `history-*.jsonl` files in the
  state dir, size-gated (16 KiB and 16 MiB). It never reads the config. It has no
  network access, writes nothing and spawns no processes. The service start
  command it shows when stale is display text only and is never executed. It
  finds the state dir like the service does (`POWER_WATCH_STATE_DIR`, else
  `XDG_STATE_HOME`), as seen by the shell process, so set the variable in both or
  neither.
- **Removal.** `./install.sh --user --uninstall`, or by hand: `omarchy plugin
  disable io.github.rsd.power-watch`, remove the symlink in
  `~/.config/omarchy/plugins/`, and run `omarchy-shell shell rescanPlugins`.
  Nothing needs to be stopped first.

Move the widget with `omarchy bar move io.github.rsd.power-watch --section
right` (see `omarchy bar --help`). The shell caches QML: after editing plugin
code, run `omarchy restart shell`.

**Known limitation.** The plugin lives in `omarchy-plugin/` of this repository.
A marketplace submission would need the plugin at a repository root, which this
layout does not provide.

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

Needs only the standard library and pytest. `tests/plugin/model-test.js` (the
plugin's model logic) needs `node`, and its pytest wrapper skips without it; the
QML lint test skips without `qmllint`. Fixtures are injected through
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
8. `./install.sh --user`. Expect the plugin linked and enabled with no warnings,
   and `systemctl --user status power-watch` active.
   - Reboot and check whether `energy_uj` kept its mode. If not, re-apply the
     tmpfiles rule (see Install).
   - The bar shows watts within a few seconds. Stop the service: it dims within
     about 10 s plus 3 x interval. Start it: watts return.
   - Open the panel and switch 1h / 24h / 7d. `power-watch history --json`
     should agree with Max, Average and Current.
   - Unplug the adapter for a minute and replug: the chart shows a battery band.
     Stop the service for a minute: a gap appears.
   - Set a low `--threshold` via `systemctl --user edit power-watch` under load:
     expect the normal, approaching and over colours.
   - `./install.sh --user --uninstall`: the widget disappears and the service
     stops.

## License

MIT. See `LICENSE`; the plugin carries its own MIT `LICENSE` in `omarchy-plugin/`.
