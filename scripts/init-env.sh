#!/usr/bin/env bash
# Create .env from .env.example with a strong random password, so `make up` works on a fresh clone.
# Does nothing if .env already exists.
set -euo pipefail

here="$(cd "$(dirname "$0")/.." && pwd)"
[ -f "$here/.env" ] && exit 0

password="$(python3 -c '
import secrets, string
pool = string.ascii_letters + string.digits
core = "".join(secrets.choice(pool) for _ in range(14))
print("O2-" + core + "-" + secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ") + secrets.choice("abcdefghijkmnpqrstuvwxyz") + str(secrets.randbelow(10)) + "!")
')"
sed "s|^ZO_ROOT_USER_PASSWORD=.*|ZO_ROOT_USER_PASSWORD=$password|" "$here/.env.example" > "$here/.env"
chmod 600 "$here/.env"
email="$(grep -E '^ZO_ROOT_USER_EMAIL=' "$here/.env" | cut -d= -f2-)"
echo "Created .env with a generated password."
echo "  Login:    $email"
echo "  Password: $password"
echo "  (stored in .env; OpenObserve reads it on the first start only)"
