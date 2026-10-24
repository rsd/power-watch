#!/usr/bin/env bash
# install.sh — install power-watch, power-cap, the RAPL tmpfiles rule and the user unit.
#
#   sudo ./install.sh              install and apply the tmpfiles rule now
#   sudo ./install.sh --group NAME   same, granting the RAPL read access to group NAME
#   sudo ./install.sh --uninstall  remove the installed files
#   ./install.sh --user            as your user: link the Omarchy bar plugin, enable and
#                                  (re)start the power-watch user service, enable the plugin
#   ./install.sh --user --uninstall  disable the plugin and the service, remove the link
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
#   OMARCHY_PLUGIN_DIR  Omarchy shell plugin dir (default ~/.config/omarchy/plugins)
#   SYSTEMCTL           systemctl command (default systemctl)
#   OMARCHY             omarchy command (default omarchy; skipped if not on PATH)
#   OMARCHY_SHELL       omarchy-shell command, for `shell rescanPlugins` (default omarchy-shell)
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
    plugin_dir=${OMARCHY_PLUGIN_DIR:-$HOME/.config/omarchy/plugins}
    link=$plugin_dir/$plugin_id
    target=$src/omarchy-plugin
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
    # The running shell only sees a new plugin dir after a rescan (omarchy-plugin-clone and
    # omarchy-plugin-remove do the same); fails when the shell is not running.
    rescan_plugins() {
        "$OMARCHY_SHELL" shell rescanPlugins >/dev/null 2>&1
    }
    plugin_discovered() {
        "$OMARCHY" plugin list --json 2>/dev/null |
            grep -Eq "\"id\"[[:space:]]*:[[:space:]]*\"$plugin_id_re\""
    }
    # like omarchy-plugin-clone: rescan, then wait up to 2 s (40 x 50 ms) for discovery
    wait_for_discovery() {
        attempt=0
        while [ "$attempt" -lt 40 ]; do
            plugin_discovered && return 0
            sleep 0.05
            attempt=$((attempt + 1))
        done
        return 1
    }

    if [ "$mode" = uninstall ]; then
        if have_omarchy; then
            "$OMARCHY" plugin disable "$plugin_id" ||
                warn "disabling the Omarchy plugin failed" "omarchy plugin disable $plugin_id"
        else
            echo "omarchy not found; if the bar widget is enabled, run: omarchy plugin disable $plugin_id"
        fi
        "$SYSTEMCTL" --user disable --now "$unit_name" ||
            warn "disabling the service failed" "systemctl --user disable --now $unit_name"
        if [ -L "$link" ]; then
            rm -f "$link"
            echo "Removed $link"
            # a shell that is not running finds no plugin dir when it starts: nothing to do
            rescan_plugins ||
                echo "Omarchy shell not reachable; it drops the plugin when it next starts."
        elif [ -e "$link" ]; then
            echo "install.sh: $link is not a symlink; left in place" >&2
        fi
        exit "$status"
    fi

    linked=
    if [ ! -d "$target" ]; then
        warn "$target is missing, Omarchy plugin not linked" \
             "./install.sh --user from a complete power-watch checkout"
    elif [ -L "$link" ] || [ ! -e "$link" ]; then
        if mkdir -p "$plugin_dir" && ln -sfn "$target" "$link"; then
            echo "Linked $link -> $target"
            linked=1
        else
            warn "linking the Omarchy plugin failed" "ln -sfn $target $link"
        fi
    else
        warn "$link exists and is not a symlink; refusing to replace it" \
             "move it away, then ./install.sh --user"
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
    if [ -z "$linked" ]; then
        echo "Omarchy plugin not linked; not enabling it."
    elif ! have_omarchy; then
        echo "omarchy not found; to add the bar widget run: omarchy plugin enable $plugin_id"
    elif ! rescan_plugins; then
        warn "the Omarchy shell did not rescan its plugins (is it running?); bar widget not enabled" \
             "omarchy-shell shell rescanPlugins && omarchy plugin enable $plugin_id"
    elif ! wait_for_discovery; then
        warn "the Omarchy shell has not discovered $plugin_id after 2 s; bar widget not enabled" \
             "omarchy plugin enable $plugin_id"
    else
        "$OMARCHY" plugin enable "$plugin_id" ||
            warn "enabling the Omarchy plugin failed" "omarchy plugin enable $plugin_id"
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
  ./install.sh --user         start the background service (alerts, history) and add the Omarchy bar widget
  power-watch                 check the live estimate
  power-watch calibrate       measure the rest-of-system baseline
  power-cap status            show current CPU/GPU limits
EOF
exit "$status"
