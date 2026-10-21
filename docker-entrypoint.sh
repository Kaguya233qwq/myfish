#!/bin/sh
set -eu

if [ "${1:-}" = "myfish" ] && [ "${2:-}" = "run" ]; then
    myfish sync
    shift
    exec ./.myfish/venv/bin/myfish "$@"
fi

exec "$@"
