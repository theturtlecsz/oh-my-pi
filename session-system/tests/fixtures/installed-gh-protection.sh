#!/bin/sh
# Disposable external GitHub-protection response; no CLI/session code is replaced.
if [ "$#" -eq 4 ] && [ "$1" = "api" ] && [ "$2" = 'repos/{owner}/{repo}/branches/main/protection/required_status_checks' ] && [ "$3" = '--jq' ] && [ "$4" = '.contexts | length' ]; then
    printf '12\n'
else
    printf 'Unsupported installed qualification GitHub operation\n' >&2
    exit 64
fi
