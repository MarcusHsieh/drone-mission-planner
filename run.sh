#!/usr/bin/env bash
# One-command startup. Usage: ./run.sh   (or: ./run.sh --dev)
exec node "$(dirname "$0")/run.mjs" "$@"
