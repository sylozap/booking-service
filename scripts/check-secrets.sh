#!/usr/bin/env bash
# Secrets never enter git: the repository carries .env.example and nothing else.
# Rotating a leaked key is expensive, and history keeps it after a revert.
set -euo pipefail

status=0

tracked_env=$(git ls-files -- '.env' '.env.*' ':!.env.example' || true)
if [[ -n "${tracked_env}" ]]; then
    echo "environment files are tracked by git:" >&2
    echo "${tracked_env}" >&2
    status=1
fi

patterns='BEGIN (RSA |EC |OPENSSH |PGP )?PRIVATE KEY|AKIA[0-9A-Z]{16}|(bot)?[0-9]{8,10}:AA[A-Za-z0-9_-]{33}'
if git ls-files -z -- ':!docs' ':!*.lock' \
    | xargs -0 --no-run-if-empty grep -InE "${patterns}" >&2; then
    echo "the lines above look like a secret" >&2
    status=1
fi

exit "${status}"
