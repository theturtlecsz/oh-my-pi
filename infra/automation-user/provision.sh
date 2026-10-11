#!/usr/bin/env bash
# Provision a dedicated automation user: the account, its home, and lingering.
# Sudo is not installed here. The only sudo rule is the owner-action gateway
# (infra/automation-user/omp-owner-action.sudoers), installed separately.
#
#   provision.sh --user U --owner O --repo-url URL [--render-only --out DIR]
#
# --render-only writes plan.sh under DIR and does not change the system.
# Without --render-only the same plan is applied and requires root.
# The plan never usermod, gpasswd, chown, chmod, or setfacl the owner account.
set -euo pipefail

die() {
  echo "$*" >&2
  exit 2
}

replace_literal() {
  local s=$1 find=$2 repl=$3 out="" glob=0
  case $- in *f*) glob=1 ;; esac
  set -f
  while [ -n "$s" ]; do
    case $s in
      *"$find"*)
        out="${out}${s%%"$find"*}"
        out="${out}${repl}"
        s=${s#*"$find"}
        ;;
      *)
        out="${out}${s}"
        s=""
        ;;
    esac
  done
  if [ "$glob" -eq 0 ]; then
    set +f
  fi
  printf '%s' "$out"
}

write_plan() {
  local dest=$1 text
  text=$(cat <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
umask 022
user='@USER@'
owner='@OWNER@'
repo_url='@REPO_URL@'
if [ "$(id -u)" -ne 0 ]; then
  echo "apply requires root" >&2
  exit 2
fi
id -u "$owner" >/dev/null
useradd -m -U -s /bin/bash -- "$user"
home=$(getent passwd "$user" | cut -d: -f6)
owner_home=$(getent passwd "$owner" | cut -d: -f6)
case $home in
  /*) ;;
  *) echo "refusing non-absolute home" >&2; exit 2 ;;
esac
if [ -z "$owner_home" ] || [ "$home" = "$owner_home" ] || [ "$home" = / ]; then
  echo "refusing home" >&2
  exit 2
fi
if [[ "$home" == "$owner_home"/* ]]; then
  echo "refusing home" >&2
  exit 2
fi
chmod 700 -- "$home"
loginctl enable-linger "$user"
runuser -u "$user" -- git clone -- "$repo_url" "$home/oh-my-pi"
mkdir -p -- "$home/.config/systemd/user"
chown -- "$user:$user" "$home/.config" "$home/.config/systemd" "$home/.config/systemd/user"
chmod 0755 -- "$home/.config" "$home/.config/systemd" "$home/.config/systemd/user"
EOF
)
  text=$(replace_literal "$text" "@USER@" "$user")
  text=$(replace_literal "$text" "@OWNER@" "$owner")
  text=$(replace_literal "$text" "@REPO_URL@" "$repo_url")
  printf '%s\n' "$text" > "$dest"
}

valid_name() {
  local name=$1
  case $name in
    [A-Za-z_][A-Za-z0-9_-]*) [ "${#name}" -le 32 ] ;;
    *) return 1 ;;
  esac
}

user=""
owner=""
repo_url=""
render_only=0
out=""
while [ "$#" -gt 0 ]; do
  case $1 in
    --user) user=${2:?--user needs a value}; shift 2 ;;
    --owner) owner=${2:?--owner needs a value}; shift 2 ;;
    --repo-url) repo_url=${2:?--repo-url needs a value}; shift 2 ;;
    --render-only) render_only=1; shift ;;
    --out) out=${2:?--out needs a directory}; shift 2 ;;
    *) die "unknown argument: $1" ;;
  esac
done

[ -n "$user" ] && [ -n "$owner" ] && [ -n "$repo_url" ] || die "--user, --owner, and --repo-url are required"
if [ "$user" = "$owner" ]; then
  die "refusing user equal to owner"
fi
valid_name "$user" || die "invalid --user"
valid_name "$owner" || die "invalid --owner"
case $user in
  root|ALL) die "refusing user $user" ;;
esac
case $repo_url in
  ''|*[[:space:]]*|*"'"*|*'\'*|*@USER@*|*@OWNER@*|*@REPO_URL@*) die "invalid --repo-url" ;;
esac
if [ "$render_only" -eq 1 ] && [ -z "$out" ]; then
  die "--render-only requires --out"
fi
if [ "$render_only" -eq 0 ] && [ -n "$out" ]; then
  die "--out requires --render-only"
fi
case $out in
  *$'\n'*|*$'\r'*) die "--out must not contain line breaks" ;;
esac

if ! owner_uid=$(id -u "$owner" 2>/dev/null); then
  die "unknown owner"
fi
if user_uid=$(id -u "$user" 2>/dev/null); then
  if [ "$user_uid" -lt 1000 ]; then
    die "refusing uid below 1000"
  fi
  if [ "$user_uid" = "$owner_uid" ]; then
    die "refusing user that is the owner"
  fi
fi
if [ "$render_only" -eq 0 ] && [ "$(id -u)" -ne 0 ]; then
  die "apply requires root"
fi

if [ "$render_only" -eq 1 ]; then
  dest=$out
  mkdir -p -- "$dest"
else
  dest=$(mktemp -d)
  trap 'rm -rf -- "$dest"' EXIT
fi
write_plan "$dest/plan.sh"
chmod 0755 -- "$dest/plan.sh"
if [ "$render_only" -eq 1 ]; then
  exit 0
fi
bash "$dest/plan.sh"
