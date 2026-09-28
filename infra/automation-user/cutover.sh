#!/usr/bin/env bash
# Move flood's automation from the owner's user units to the restricted
# automation user, with a written rollback. Run by the human owner (Chris) per
# docs/automation-user.md; never by an agent.
#
#   cutover.sh plan     --user U --state-file F [--units TSV]
#   cutover.sh apply    --user U --state-file F [--units TSV]
#   cutover.sh rollback --user U --state-file F [--units TSV]
#
# The units TSV is tab-separated `side\tunit`. `plan` prints only. `apply`
# snapshots the owner units' is-enabled/is-active into F, stops and disables
# them, then enables and starts the automation user's units; a unit still
# inactive after the switch is a failure that asks for rollback. `rollback`
# stops and disables the automation units and restores every owner unit to its
# snapshot exactly, then records rolled_back_at in F; it is idempotent.
set -euo pipefail

SELF=${BASH_SOURCE[0]}

usage() {
	echo "usage: cutover.sh {plan|apply|rollback} --user U --state-file F [--units TSV]" >&2
}

action=""
user=""
state_file=""
units_file=""

while [ "$#" -gt 0 ]; do
	case "$1" in
		plan | apply | rollback)
			if [ -n "$action" ]; then
				echo "cutover.sh: more than one action: $action $1" >&2
				usage
				exit 2
			fi
			action=$1
			shift
			;;
		--user)
			user=${2:?--user needs a value}
			shift 2
			;;
		--state-file)
			state_file=${2:?--state-file needs a value}
			shift 2
			;;
		--units)
			units_file=${2:?--units needs a value}
			shift 2
			;;
		-h | --help)
			usage
			exit 0
			;;
		*)
			echo "cutover.sh: unknown argument: $1" >&2
			usage
			exit 2
			;;
	esac
done

if [ -z "$action" ] || [ -z "$user" ] || [ -z "$state_file" ]; then
	usage
	exit 2
fi

case "$user" in
	'' | *[!A-Za-z0-9._-]*) echo "cutover.sh: invalid user: $user" >&2; exit 2 ;;
esac
case "$state_file" in /*) ;; *) echo "cutover.sh: --state-file must be an absolute path" >&2; exit 2 ;; esac

if [ -z "$units_file" ]; then
	units_file="$(cd -- "$(dirname -- "$SELF")" && pwd)/cutover-units.tsv"
fi
[ -r "$units_file" ] || { echo "cutover.sh: units file not readable: $units_file" >&2; exit 2; }

owner_units=()
automation_units=()
seen_owner=" "
seen_automation=" "
while IFS=$'\t' read -r side unit rest; do
	[ -n "$side" ] || continue
	case "$side" in \#*) continue ;; esac
	if [ -n "${rest:-}" ]; then
		echo "cutover.sh: units line has more than two fields: $side $unit $rest" >&2
		exit 2
	fi
	[ -n "$unit" ] || { echo "cutover.sh: units line has no unit" >&2; exit 2; }
	case "$side" in
		owner)
			case "$seen_owner" in *" $unit "*) echo "cutover.sh: duplicate owner unit: $unit" >&2; exit 2 ;; esac
			seen_owner="$seen_owner$unit "
			owner_units+=("$unit")
			;;
		automation)
			case "$seen_automation" in *" $unit "*) echo "cutover.sh: duplicate automation unit: $unit" >&2; exit 2 ;; esac
			seen_automation="$seen_automation$unit "
			automation_units+=("$unit")
			;;
		*) echo "cutover.sh: unknown side '$side' for $unit" >&2; exit 2 ;;
	esac
done <"$units_file"
[ "${#owner_units[@]}" -gt 0 ] || { echo "cutover.sh: units file has no owner units: $units_file" >&2; exit 2; }
[ "${#automation_units[@]}" -gt 0 ] || { echo "cutover.sh: units file has no automation units: $units_file" >&2; exit 2; }

# The automation user's manager. `sudo ... -M U@` targets that user's own
# systemd --user instance without us running anything inside their login.
remote() { sudo systemctl --user -M "$user@" "$@"; }

# Owner state is read and printed by the owner's own manager; `-M` needs root.
owner() { systemctl --user "$@"; }

# Write the state file atomically, so a crash cannot leave a half snapshot.
state_write() {
	local tmp
	tmp=$(mktemp)
	cat >"$tmp"
	mv -f "$tmp" "$state_file"
}

json_string() { python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$1"; }

# Print one `["unit","is_enabled","is_active"]` line per snapshot entry.
json_units() {
	python3 - "$state_file" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    state = json.load(handle)
units = state.get("units")
if not isinstance(units, dict) or not units:
    sys.exit(1)
for name, entry in units.items():
    if not isinstance(entry, dict):
        sys.exit(1)
    print(json.dumps([name, str(entry.get("is_enabled", "")), str(entry.get("is_active", ""))]))
PY
}

# is-enabled/is-active print the state and exit non-zero for `disabled` and
# `inactive`, so the printed word is a valid reading: only an empty output means
# the state could not be read.
query_state() {
	local unit=$1 kind=$2 probe=$3 value
	value=$("$probe" "$kind" "$unit" 2>/dev/null || true)
	[ -n "$value" ] || return 1
	printf '%s' "$value"
}

# Same reading for display: keep the printed word, `unknown` only when empty.
format_state() {
	local value
	value=$("$@" 2>/dev/null || true)
	[ -n "$value" ] || value=unknown
	printf '%s' "$value"
}

# Record the owner units' current state as F (JSON). The state file must not
# exist yet: apply never overwrites a snapshot it did not create.
capture_owner_state() {
	local unit enabled active entries=() entry name
	for unit in "${owner_units[@]}"; do
		enabled=$(query_state "$unit" is-enabled owner) || {
			echo "cutover.sh: cannot read is-enabled for owner unit $unit" >&2
			exit 1
		}
		active=$(query_state "$unit" is-active owner) || {
			echo "cutover.sh: cannot read is-active for owner unit $unit" >&2
			exit 1
		}
		entries+=("$(printf '%s\t%s\t%s' "$unit" "$enabled" "$active")")
	done
	{
		printf '{\n'
		printf '  "user": %s,\n' "$(json_string "$user")"
		printf '  "version": 1,\n'
		printf '  "captured_at": %s,\n' "$(json_string "$(date -u +%Y-%m-%dT%H:%M:%SZ)")"
		printf '  "units": {'
		local first=1
		for entry in "${entries[@]}"; do
			IFS=$'\t' read -r name enabled active <<<"$entry"
			if [ "$first" -eq 1 ]; then
				printf '\n'
				first=0
			else
				printf ',\n'
			fi
			printf '    %s: {"is_enabled": %s, "is_active": %s}' \
				"$(json_string "$name")" "$(json_string "$enabled")" "$(json_string "$active")"
		done
		printf '\n  }\n'
		printf '}\n'
	} | state_write
}

# Put one owner unit back into its snapshot state. `enable`/`disable` restore
# the is-enabled axis, `start`/`stop` the is-active one. Units with no
# enablement state (`static`, a unit that is gone) only get the active axis.
# A command that fails is fatal: the caller must report a partial restore, not
# a successful one.
restore_owner_unit() {
	local unit=$1 enabled=$2 active=$3
	case "$enabled" in
		enabled | enabled-runtime | linked | linked-runtime | alias | indirect)
			owner enable "$unit" >/dev/null || return 1
			;;
		disabled | masked | masked-runtime)
			owner disable "$unit" >/dev/null || return 1
			;;
		static | generated | transient | not-found | bad) ;;
		*)
			echo "cutover.sh: unknown saved is-enabled '$enabled' for $unit" >&2
			return 1
			;;
	esac
	case "$active" in
		active | activating | reloading)
			owner start "$unit" >/dev/null || return 1
			;;
		inactive | deactivating | failed)
			owner stop "$unit" >/dev/null || return 1
			;;
		unknown | not-found) ;;
		*)
			echo "cutover.sh: unknown saved is-active '$active' for $unit" >&2
			return 1
			;;
	esac
}

# Restore every owner unit. Returns non-zero if any unit could not be restored,
# so the caller can leave the state file without rolled_back_at.
restore_owner_state() {
	local snapshot line name enabled active failed=""
	if ! snapshot=$(json_units); then
		echo "cutover.sh: state file F is unreadable: $state_file" >&2
		return 1
	fi
	while IFS= read -r line; do
		name=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])[0])' "$line")
		enabled=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])[1])' "$line")
		active=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])[2])' "$line")
		if ! restore_owner_unit "$name" "$enabled" "$active"; then
			failed="${failed:+$failed }$name"
		fi
	done <<<"$snapshot"
	if [ -n "$failed" ]; then
		echo "cutover.sh: could not restore owner unit(s): $failed" >&2
		return 1
	fi
}

add_rolled_back_at() {
	local tmp
	tmp=$(mktemp)
	python3 - "$state_file" "$tmp" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" <<'PY'
import json
import sys

path, tmp, stamp = sys.argv[1], sys.argv[2], sys.argv[3]
with open(path, encoding="utf-8") as handle:
    state = json.load(handle)
state["rolled_back_at"] = stamp
with open(tmp, "w", encoding="utf-8") as handle:
    json.dump(state, handle, indent=2)
    handle.write("\n")
PY
	mv -f "$tmp" "$state_file"
}

# --- plan ----------------------------------------------------------------

if [ "$action" = plan ]; then
	echo "cutover.sh plan: user=$user state-file=$state_file units=$units_file"
	echo "owner units (stopped and disabled by apply; state saved to F):"
	for unit in "${owner_units[@]}"; do
		printf '  owner\t%s\t%s\t%s\n' "$unit" \
			"$(format_state owner is-enabled "$unit")" \
			"$(format_state owner is-active "$unit")"
	done
	echo "automation units (enabled and started by apply; enabled-and-active checked after):"
	for unit in "${automation_units[@]}"; do
		printf '  automation\t%s\t%s\t%s\n' "$unit" \
			"$(format_state remote is-enabled "$unit")" \
			"$(format_state remote is-active "$unit")"
	done
	echo "nothing was changed"
	exit 0
fi

# --- apply ---------------------------------------------------------------

if [ "$action" = apply ]; then
	if [ -e "$state_file" ]; then
		echo "cutover.sh: refusing: state file already exists: $state_file" >&2
		echo "cutover.sh: roll it back first, or remove a stale file after checking it" >&2
		exit 1
	fi

	missing=""
	for unit in "${automation_units[@]}"; do
		# `remote cat` fails for a unit the automation user does not have
		# installed; a missing unit would fail the post-check anyway.
		if ! remote cat "$unit" >/dev/null 2>&1; then
			if [ -z "$missing" ]; then missing=$unit; else missing="$missing $unit"; fi
		fi
	done
	if [ -n "$missing" ]; then
		echo "cutover.sh: refusing: automation unit(s) missing for $user: $missing" >&2
		echo "cutover.sh: provision the automation user and its units first (docs/automation-user.md)" >&2
		exit 1
	fi

	if [ -n "$(docker ps --filter name=robomp --format '{{.Names}}' 2>/dev/null || true)" ]; then
		echo "cutover.sh: refusing: an owner robomp container is running; stop it before cutover" >&2
		exit 1
	fi

	capture_owner_state
	echo "cutover.sh: saved owner unit state to $state_file"

	for unit in "${owner_units[@]}"; do
		owner disable --now "$unit" >/dev/null 2>&1 || true
	done

	for unit in "${automation_units[@]}"; do
		remote enable --now "$unit" >/dev/null 2>&1 || true
	done

	# Both halves must match the cutover's intent before the switch is called
	# good: owner stopped and disabled, automation enabled and active. The
	# automation half alone is not enough — a failed owner stop would leave
	# flood running under both users. An unreadable state prints `unknown` and
	# so fails the check too.
	owner_failed=""
	automation_failed=""
	for unit in "${owner_units[@]}"; do
		if [ "$(format_state owner is-active "$unit")" != inactive ]; then
			owner_failed="${owner_failed:+$owner_failed }$unit"
		fi
		if [ "$(format_state owner is-enabled "$unit")" != disabled ]; then
			owner_failed="${owner_failed:+$owner_failed }$unit"
		fi
	done
	for unit in "${automation_units[@]}"; do
		if [ "$(format_state remote is-active "$unit")" != active ]; then
			automation_failed="${automation_failed:+$automation_failed }$unit"
		fi
		if [ "$(format_state remote is-enabled "$unit")" != enabled ]; then
			automation_failed="${automation_failed:+$automation_failed }$unit"
		fi
	done
	if [ -n "$owner_failed" ] || [ -n "$automation_failed" ]; then
		[ -z "$owner_failed" ] || echo "cutover.sh: post-check failed: owner unit(s) not stopped: $owner_failed" >&2
		[ -z "$automation_failed" ] || echo "cutover.sh: post-check failed: automation unit(s) not enabled+active: $automation_failed" >&2
		echo "run: cutover.sh rollback --state-file $state_file"
		exit 1
	fi

	echo "cutover.sh: cutover done; $user now runs the automation units"
	exit 0
fi

# --- rollback ------------------------------------------------------------

if [ "$action" = rollback ]; then
	if [ ! -e "$state_file" ]; then
		echo "cutover.sh: refusing: no state file to roll back from: $state_file" >&2
		exit 1
	fi

	# Read the snapshot before touching anything. json_units does not use the
	# shell's set -e (it is a command substitution), so a corrupt file must be
	# checked here — otherwise the automation units are already stopped before
	# the failure is noticed and the owner stays unrestored.
	if ! json_units >/dev/null; then
		echo "cutover.sh: refusing: state file F is unreadable: $state_file" >&2
		exit 1
	fi

	automation_running=""
	for unit in "${automation_units[@]}"; do
		remote disable --now "$unit" >/dev/null 2>&1 \
			|| automation_running="${automation_running:+$automation_running }$unit"
	done

	if ! restore_owner_state; then
		echo "cutover.sh: rollback incomplete; state file left unchanged: $state_file" >&2
		exit 1
	fi

	add_rolled_back_at
	if [ -n "$automation_running" ]; then
		echo "cutover.sh: rollback done, but automation unit(s) could not be stopped: $automation_running" >&2
	fi
	echo "cutover.sh: rollback done; owner units restored from $state_file"
	exit 0
fi
