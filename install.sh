#!/usr/bin/env bash
# install.sh — install power-watch, power-cap, the RAPL tmpfiles rule and the user unit.
#
#   sudo ./install.sh              install and apply the tmpfiles rule now
#   sudo ./install.sh --group NAME   same, granting the RAPL read access to group NAME
#   sudo ./install.sh --uninstall  remove the installed files
#   ./install.sh --user            as your user: enable and (re)start the power-watch user
#                                  service, then offer the Omarchy bar widget, a separate
#                                  plugin (github.com/rsd/omarchy-power-watch), when omarchy
#                                  is present and stdin/stdout are a terminal
#   ./install.sh --user --uninstall  disable the service (a separately installed plugin is left)
#
# --user is a separate step run as the normal user (it refuses root) because
# `systemctl --user` needs the user's session bus, which a root/sudo context does not have.
#
# The tmpfiles rule makes the RAPL energy counters (energy_uj) readable by one
# group: --group NAME, else $RAPL_GROUP, else `wheel` if it exists, else `sudo`;
# with neither, the install stops and asks for --group. The installed
# /etc/tmpfiles.d/power-watch.conf carries the chosen group (the repo copy names
# wheel). The config example goes to $PREFIX/share/power-watch/config.example; a
# user config is never created or overwritten.
#
# Trade-off: the kernel keeps energy_uj root-only since CVE-2020-8694 ("Platypus"),
# because fine-grained energy readings are a power side channel that can leak
# secrets from other processes. Re-opening it to a group is acceptable when that
# group already means "can become root" (wheel or sudo with sudo rights); otherwise
# choose a narrower group with --group (e.g. a dedicated one holding only the users
# who run power-watch).
#
# Environment:
#   PREFIX            install prefix (default /usr/local)
#                     (the installed unit's ExecStart follows it)
#   DESTDIR           staging root (default empty = the live system; no root check)
#   SYSTEMD_TMPFILES  command that applies the rule (default systemd-tmpfiles; with
#                     DESTDIR set and this unset, the rule is not applied at all)
#   RAPL_GROUP        group for the tmpfiles rule (--group wins over it; empty = unset)
#   GETENT            getent command used to check groups (default getent)
# --user only:
#   OMARCHY_PLUGIN_DIR  Omarchy shell plugin dir, to see whether the bar widget is installed
#                       (default ~/.config/omarchy/plugins, where `omarchy plugin add` puts it)
#   SYSTEMCTL           systemctl command (default systemctl)
#   OMARCHY             omarchy command (default omarchy; skipped if not on PATH)
#   OMARCHY_SHELL       omarchy-shell command, for `shell rescanPlugins` after the old bundled
#                       plugin link is removed (default omarchy-shell; skipped if not on PATH)
#   POWER_WATCH_INTERACTIVE  1 or 0: whether to run `omarchy plugin add` (it asks you to
#                       confirm), overriding the check that stdin and stdout are a terminal
set -eu

PREFIX=${PREFIX:-/usr/local}
DESTDIR=${DESTDIR:-}
if [ -n "$DESTDIR" ] && [ -z "${SYSTEMD_TMPFILES+set}" ]; then
    SYSTEMD_TMPFILES=
else
    SYSTEMD_TMPFILES=${SYSTEMD_TMPFILES:-systemd-tmpfiles}
fi

GETENT=${GETENT:-getent}

src=$(cd "$(dirname "$0")" && pwd)
bin_dir=$DESTDIR$PREFIX/bin
tmpfiles_dir=$DESTDIR/etc/tmpfiles.d
unit_dir=$DESTDIR$PREFIX/lib/systemd/user
share_dir=$DESTDIR$PREFIX/share/power-watch
conf=$tmpfiles_dir/power-watch.conf
unit=$unit_dir/power-watch.service
example=$share_dir/config.example

usage() {
    echo "usage: sudo ./install.sh [--group NAME] [--uninstall]"
    echo "       ./install.sh --user [--uninstall]   (as your user, not root)"
}

orig_args="$*"
mode=install
user_mode=
group=${RAPL_GROUP:-}
group_from=${RAPL_GROUP:+RAPL_GROUP}
group_flag=
while [ "$#" -gt 0 ]; do
    case $1 in
        --uninstall) mode=uninstall ;;
        --user) user_mode=1 ;;
        --group)
            if [ "$#" -lt 2 ] || [ -z "$2" ]; then
                echo "install.sh: --group needs a group name" >&2
                usage >&2
                exit 2
            fi
            group=$2 group_from=--group group_flag=1
            shift ;;
        --group=*)
            group=${1#--group=} group_from=--group group_flag=1
            if [ -z "$group" ]; then
                echo "install.sh: --group needs a group name" >&2
                usage >&2
                exit 2
            fi ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; exit 2 ;;
    esac
    shift
done
if [ -n "$user_mode" ] && [ -n "$group_flag" ]; then
    echo "install.sh: --group applies to the system install (sudo ./install.sh), not --user" >&2
    exit 2
fi

if [ -n "$user_mode" ]; then
    SYSTEMCTL=${SYSTEMCTL:-systemctl}
    OMARCHY=${OMARCHY:-omarchy}
    OMARCHY_SHELL=${OMARCHY_SHELL:-omarchy-shell}
    plugin_id=io.github.rsd.power-watch
    plugin_id_re='io\.github\.rsd\.power-watch'
    plugin_url=https://github.com/rsd/omarchy-power-watch
    plugin_add="omarchy plugin add $plugin_url --enable"
    plugin_dir=${OMARCHY_PLUGIN_DIR:-$HOME/.config/omarchy/plugins}
    plugin_path=$plugin_dir/$plugin_id
    # where install.sh used to link the plugin from, before it moved to its own repo; the
    # checkout may have been reached by another path then (a symlinked dir), hence both
    legacy_target=$src/omarchy-plugin
    legacy_target_phys=$(cd "$src" && pwd -P)/omarchy-plugin
    unit_name=power-watch.service

    if [ "$(id -u)" -eq 0 ]; then
        again="./install.sh --user"
        [ "$mode" = uninstall ] && again="$again --uninstall"
        echo "install.sh: --user must run as your normal user, not root (systemctl --user needs" \
             "your session bus): $again" >&2
        exit 1
    fi

    status=0
    warn() {
        echo "install.sh: $1; retry: $2" >&2
        status=1
    }
    have_omarchy() {
        command -v "$OMARCHY" >/dev/null 2>&1
    }
    # stdin and stdout on a terminal: omarchy's own trust prompt can ask
    interactive() {
        case ${POWER_WATCH_INTERACTIVE:-} in
            1) return 0 ;;
            0) return 1 ;;
        esac
        [ -t 0 ] && [ -t 1 ]
    }
    # The link the old installer made: into this checkout by either path, or dangling into
    # some .../omarchy-plugin (the dir is gone once the checkout is past the repo split).
    plugin_is_legacy_link() {
        [ -L "$plugin_path" ] || return 1
        link_target=$(readlink "$plugin_path")
        case $link_target in
            "$legacy_target"|"$legacy_target_phys") return 0 ;;
            */omarchy-plugin) [ ! -e "$plugin_path" ] ;;
            *) return 1 ;;
        esac
    }
    # `omarchy plugin list --json`, whitespace tolerated, names the plugin (in any plugin dir)
    plugin_listed() {
        "$OMARCHY" plugin list --json 2>/dev/null |
            grep -Eq "\"id\"[[:space:]]*:[[:space:]]*\"$plugin_id_re\""
    }
    # Disable the old bundled plugin, drop the link, then have a running shell rescan so no
    # stale entry is left. Disabling needs a shell that still knows the plugin, so it fails in
    # the usual upgrade (target gone, or no shell running): information, not an error.
    remove_legacy_link() {
        plugin_is_legacy_link || return 0
        if have_omarchy && ! "$OMARCHY" plugin disable "$plugin_id"; then
            echo "Could not disable the old bundled plugin (no shell running, or it is already" \
                 "gone); removing its link anyway."
        fi
        rm -f "$plugin_path"
        echo "Removed the old bundled plugin link $plugin_path -> $link_target"
        if command -v "$OMARCHY_SHELL" >/dev/null 2>&1 &&
            ! "$OMARCHY_SHELL" shell rescanPlugins >/dev/null 2>&1; then
            echo "Omarchy shell not reachable; it drops the old plugin when it next starts."
        fi
    }

    remove_legacy_link
    if [ "$mode" = uninstall ]; then
        "$SYSTEMCTL" --user disable --now "$unit_name" ||
            warn "disabling the service failed" "systemctl --user disable --now $unit_name"
        # a separately installed plugin is omarchy's to manage
        if [ -e "$plugin_path" ] || [ -L "$plugin_path" ]; then
            echo "The Omarchy bar widget is installed separately; to remove it run:" \
                 "omarchy plugin remove $plugin_id"
        fi
        exit "$status"
    fi

    "$SYSTEMCTL" --user daemon-reload ||
        warn "systemctl --user daemon-reload failed" "systemctl --user daemon-reload"
    if "$SYSTEMCTL" --user enable "$unit_name"; then
        # restart, not start: an upgrade must pick up the newly installed binary
        "$SYSTEMCTL" --user restart "$unit_name" ||
            warn "restarting the service failed" "systemctl --user restart $unit_name"
    else
        warn "enabling the service failed (did \`sudo ./install.sh\` run first?)" \
             "systemctl --user enable $unit_name && systemctl --user restart $unit_name"
    fi

    # The bar widget is a separate Omarchy plugin: offered, never installed behind your back.
    if [ -e "$plugin_path" ]; then
        echo "Omarchy bar widget already installed ($plugin_path)."
    elif have_omarchy && plugin_listed; then
        # omarchy refuses an id it already finds in another plugin dir
        echo "Omarchy bar widget already installed (omarchy plugin list reports $plugin_id)."
    elif [ -L "$plugin_path" ]; then
        warn "$plugin_path is a dangling symlink; Omarchy bar widget not added" \
             "rm $plugin_path && $plugin_add"
    elif ! have_omarchy; then
        echo "omarchy not found; for the Omarchy bar widget run: $plugin_add"
    elif ! interactive; then
        echo "Omarchy bar widget not added (no terminal to confirm); to add it run: $plugin_add"
    else
        # non-zero also when you decline omarchy's own prompt ("aborted"): not an error here
        "$OMARCHY" plugin add "$plugin_url" --enable ||
            echo "Omarchy bar widget not added; to add it later run: $plugin_add"
    fi
    exit "$status"
fi

if [ -z "$DESTDIR" ] && [ "$(id -u)" -ne 0 ]; then
    echo "install.sh: must run as root: sudo ./install.sh${orig_args:+ $orig_args}" >&2
    exit 1
fi

if [ "$mode" = uninstall ]; then
    rm -f "$bin_dir/power-watch" "$bin_dir/power-cap" "$conf" "$unit" "$example"
    rmdir "$share_dir" 2>/dev/null || true
    echo "Removed $bin_dir/power-watch, $bin_dir/power-cap, $conf, $unit, $example"
    echo "Your ~/.config/power-watch/config, if any, is left in place."
    echo "The energy_uj read access granted this boot stays until reboot (or rapl module reload)."
    echo "If you enabled the user unit, as your user run:"
    echo "  systemctl --user disable --now power-watch.service; systemctl --user daemon-reload"
    exit 0
fi

# The tmpfiles group: decided before anything is written.
have_group() {
    "$GETENT" group "$1" >/dev/null 2>&1
}
if [ -n "$group" ]; then
    case $group in
        -*|*[!A-Za-z0-9_.\$-]*)
            echo "install.sh: invalid group name '$group' (from $group_from)" >&2
            exit 2 ;;
    esac
    if ! command -v "$GETENT" >/dev/null 2>&1; then
        echo "install.sh: warning: $GETENT not found; cannot check that group $group exists" >&2
    elif ! have_group "$group"; then
        if [ -z "$DESTDIR" ]; then
            echo "install.sh: group $group (from $group_from) does not exist on this system" >&2
            exit 1
        fi
        echo "install.sh: warning: group $group does not exist here (DESTDIR staging: kept)" >&2
    fi
elif ! command -v "$GETENT" >/dev/null 2>&1; then
    echo "install.sh: cannot detect groups: $GETENT not found; pass --group NAME" \
         "(sudo ./install.sh --group NAME)" >&2
    exit 1
elif have_group wheel; then
    group=wheel group_from="auto-detected"
elif have_group sudo; then
    group=sudo group_from="auto-detected"
else
    echo "install.sh: neither a wheel nor a sudo group exists; pass the group that may read" \
         "the RAPL energy counters: sudo ./install.sh --group NAME" >&2
    exit 1
fi

mkdir -p "$bin_dir" "$tmpfiles_dir" "$unit_dir" "$share_dir"
install -m 0755 "$src/bin/power-watch" "$bin_dir/power-watch"
install -m 0755 "$src/bin/power-cap" "$bin_dir/power-cap"
install -m 0644 "$src/config.example" "$example"
# The repo rule names wheel in its Group field (type path mode user GROUP); write the
# chosen group there in the copy. The name was validated above: nothing in it is special
# to sed.
field='[^[:space:]]+[[:space:]]+'
sed -E "s/^(z[[:space:]]+$field$field$field)wheel([[:space:]]|\$)/\1$group\2/" \
    "$src/tmpfiles/power-watch.conf" > "$conf.tmp"
chmod 0644 "$conf.tmp"
mv -f "$conf.tmp" "$conf"
# The repo unit names /usr/local; point it at the actual $PREFIX in the copy.
prefix_sed=$(printf '%s' "$PREFIX" | sed 's/[\\&|]/\\&/g')
sed -e "s|/usr/local/bin/power-watch|$prefix_sed/bin/power-watch|g" \
    -e "s|/usr/local/share/power-watch/|$prefix_sed/share/power-watch/|g" \
    "$src/systemd/power-watch.service" > "$unit.tmp"
chmod 0644 "$unit.tmp"
mv -f "$unit.tmp" "$unit"
echo "Installed $bin_dir/power-watch, $bin_dir/power-cap, $conf, $unit, $example"
echo "RAPL energy counters group: $group ($group_from)"
case $PREFIX in
    /usr/local|/usr) ;;
    *) echo "install.sh: warning: $PREFIX/lib/systemd/user may not be on the systemd user unit" \
            "search path; if \`systemctl --user\` cannot find power-watch.service, link it into" \
            "~/.config/systemd/user/" >&2 ;;
esac

status=0
if [ -z "$SYSTEMD_TMPFILES" ]; then
    echo "DESTDIR set: not applying the tmpfiles rule to the running system."
elif "$SYSTEMD_TMPFILES" --create "$conf"; then
    echo "Applied $conf: RAPL energy counters are now readable by group $group."
else
    echo "install.sh: applying $conf failed; retry: sudo systemd-tmpfiles --create /etc/tmpfiles.d/power-watch.conf" >&2
    status=1
fi

cat <<EOF

Next steps (as your user, not root):
  mkdir -p ~/.config/power-watch
  cp $PREFIX/share/power-watch/config.example ~/.config/power-watch/config
                              optional: set your adapter's voltage and rating there
  ./install.sh --user         start the background service (alerts, history); offers the Omarchy bar widget
  power-watch                 check the live estimate
  power-watch calibrate       measure the rest-of-system baseline
  power-cap status            show current CPU/GPU limits
EOF
exit "$status"
