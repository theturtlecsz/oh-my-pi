#!/bin/sh
# Run one robomp compose verb against the root-owned stack for this user.
# Any other argument exits 2 before docker is invoked.
set -eu

user='@USER@'
if [ "$#" -ne 1 ]; then
  exit 2
fi
cmd=$1
case $cmd in
  up|down|restart|ps) ;;
  *) exit 2 ;;
esac

home=$(getent passwd "$user" 2>/dev/null | cut -d: -f6 || true)
if [ -n "$home" ]; then
  HOME=$home
  export HOME
  PI_ROOT=$HOME/oh-my-pi
  export PI_ROOT
fi

compose_file="/etc/${user}/robomp/docker-compose.yml"
env_file="/etc/${user}/robomp/.env"
project="robomp-${user}"

if [ "$cmd" = up ]; then
  exec docker compose -f "$compose_file" --env-file "$env_file" -p "$project" up -d --no-build
fi
exec docker compose -f "$compose_file" --env-file "$env_file" -p "$project" "$cmd"
