#!/usr/bin/env bash
# Provision a dedicated automation user that may sudo only a named robomp-ctl list.
#
#   provision.sh --user U --owner O --repo-url URL [--admin-commands F] [--render-only --out DIR]
#
# --render-only writes sudoers, the command list, robomp-ctl, robomp.service,
# docker-compose.yml, and plan.sh under DIR and does not change the system.
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

subst_file() {
  local src=$1 dest=$2 text
  text=$(cat -- "$src")
  text=$(replace_literal "$text" "@USER@" "$user")
  printf '%s\n' "$text" > "$dest"
}

trim() {
  local line=$1
  line=${line%$'\r'}
  line=${line#"${line%%[![:space:]]*}"}
  line=${line%"${line##*[![:space:]]}"}
  printf '%s' "$line"
}

validate_admin_commands() {
  local file=$1 line token base count=0
  set -f
  while IFS= read -r line || [ -n "$line" ]; do
    line=$(trim "$line")
    [ -n "$line" ] || continue
    case $line in
      *'*'*) die "refusing wildcard in admin command" ;;
      *,*|*'\'*) die "refusing comma or backslash in admin command" ;;
    esac
    set -- $line
    for token in "$@"; do
      if [ "$token" = ALL ]; then
        die "refusing ALL in admin command"
      fi
    done
    token=$1
    case $token in
      /*) ;;
      *) die "refusing relative admin command" ;;
    esac
    case $token in
      */) die "refusing directory admin command" ;;
    esac
    base=${token##*/}
    case $base in
      sh|bash|dash|ash|zsh|ksh|mksh|csh|tcsh|fish|rbash|sudo|sudoedit|su|env|systemctl|docker|docker-compose)
        die "refusing forbidden admin binary"
        ;;
    esac
    count=$((count + 1))
  done < "$file"
  set +f
  [ "$count" -ge 1 ] || die "admin command list is empty"
}

write_sudoers() {
  local dest=$1 list=$2 line first=1
  {
    printf 'Defaults:%s env_reset\n' "$user"
    printf '%s ALL=(root) NOPASSWD:' "$user"
    while IFS= read -r line || [ -n "$line" ]; do
      line=$(trim "$line")
      [ -n "$line" ] || continue
      if [ "$first" -eq 1 ]; then
        printf ' %s' "$line"
        first=0
      else
        printf ', %s' "$line"
      fi
    done < "$list"
    printf '\n'
  } > "$dest"
}

write_plan() {
  local dest=$1 text
  text=$(cat <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
umask 022
here=$(cd -- "$(dirname -- "$0")" && pwd)
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
visudo -cf "$here/sudoers"
install -o root -g root -m 0440 -- "$here/sudoers" "/etc/sudoers.d/$user"
mkdir -p -- "/etc/$user/robomp" "/usr/local/libexec/$user"
chown root:root -- "/etc/$user" "/etc/$user/robomp" "/usr/local/libexec/$user"
chmod 0755 -- "/etc/$user" "/etc/$user/robomp" "/usr/local/libexec/$user"
install -o root -g root -m 0444 -- "$here/admin-commands" "/etc/$user/admin-commands"
install -o root -g root -m 0644 -- "$here/docker-compose.yml" "/etc/$user/robomp/docker-compose.yml"
install -o root -g root -m 0755 -- "$here/robomp-ctl" "/usr/local/libexec/$user/robomp-ctl"
loginctl enable-linger "$user"
runuser -u "$user" -- git clone -- "$repo_url" "$home/oh-my-pi"
mkdir -p -- "$home/.config/systemd/user"
chown -- "$user:$user" "$home/.config" "$home/.config/systemd" "$home/.config/systemd/user"
chmod 0755 -- "$home/.config" "$home/.config/systemd" "$home/.config/systemd/user"
install -o "$user" -g "$user" -m 0644 -- "$here/robomp.service" "$home/.config/systemd/user/robomp.service"
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

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repo_root=$(cd -- "$script_dir/../.." && pwd)
commands_file=$script_dir/admin-commands.txt
ctl_template=$script_dir/robomp-ctl.sh
service_template=$script_dir/robomp.service
compose_src=$repo_root/python/robomp/docker-compose.yml

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
    --admin-commands) commands_file=${2:?--admin-commands needs a path}; shift 2 ;;
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

[ -f "$commands_file" ] || die "admin command list not found"
[ -f "$ctl_template" ] || die "robomp-ctl template not found"
[ -f "$service_template" ] || die "robomp.service template not found"
[ -f "$compose_src" ] || die "docker-compose.yml not found"

work=$(mktemp -d)
cleanup() { rm -rf -- "$work"; }
trap cleanup EXIT
subst_file "$commands_file" "$work/list"
validate_admin_commands "$work/list"

if [ "$render_only" -eq 1 ]; then
  dest=$out
  mkdir -p -- "$dest"
else
  dest=$work
fi
subst_file "$ctl_template" "$dest/robomp-ctl"
subst_file "$service_template" "$dest/robomp.service"
cp -- "$work/list" "$dest/admin-commands"
cp -- "$compose_src" "$dest/docker-compose.yml"
write_sudoers "$dest/sudoers" "$dest/admin-commands"
write_plan "$dest/plan.sh"
chmod 0755 -- "$dest/robomp-ctl" "$dest/plan.sh"
chmod 0644 -- "$dest/sudoers" "$dest/admin-commands" "$dest/robomp.service" "$dest/docker-compose.yml"

if [ "$render_only" -eq 1 ]; then
  exit 0
fi
bash "$dest/plan.sh"
