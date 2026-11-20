#!/bin/bash
# IDE container entrypoint: migrate, first-run bootstrap, then serve with daphne.
set -euo pipefail
cd /app
python manage.py migrate --noinput -v0

if [ -n "${CODE_STUDIO_ADMIN_PASSWORD:-}" ]; then
  # Only acts on an empty database; restarts never reset the password or re-add roots.
  python manage.py bootstrap --first-run-only --username "${CODE_STUDIO_ADMIN_USER:-admin}" \
    --password "$CODE_STUDIO_ADMIN_PASSWORD" --root "${CODE_STUDIO_ROOT_ALIAS:-coding}=/projects"
else
  python manage.py bootstrap --first-run-only --username "${CODE_STUDIO_ADMIN_USER:-admin}" || true
fi

exec daphne -b 0.0.0.0 -p 8000 --proxy-headers codestudio.asgi:application
