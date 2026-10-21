#!/usr/bin/env bash
set -euo pipefail

H="ds@192.168.1.17"
GW="https://192.168.1.17:8443"
CERT="$HOME/gateway-cert.pem"
FILE="$HOME/invoice1_modern.pdf"
CATEGORY="invoices"

ssh "$H" "cat ~/projects/datasmasking/.runtime/lan-tls/cert.pem" > "$CERT"
PRIVACY_API_KEY="$(ssh "$H" "grep '^PRIVACY_API_KEY=' ~/projects/datasmasking/.env | cut -d= -f2-" | tr -d '\r\n')"

curl -sS --ssl-no-revoke --cacert "$CERT" "$GW/health"
echo

curl -sS --ssl-no-revoke --cacert "$CERT" "$GW/preview/document" \
  -H "Authorization: Bearer $PRIVACY_API_KEY" \
  -F "category=$CATEGORY" \
  -F "file=@$FILE" \
  -o "$HOME/invoice1.json"

PY=$(command -v python3 || command -v python || command -v py)
"$PY" - "$HOME/invoice1.json" <<'EOF'
import json, sys
r = json.load(open(sys.argv[1], encoding="utf-8"))
print("Refused: " + r["error"] if "error" in r else r["sanitized_text"])
EOF
