#!/bin/bash
set -euo pipefail
cd "/opt/stacks/Library Site"
exec python3 scripts/_retry_cf_failed.py "$@"
