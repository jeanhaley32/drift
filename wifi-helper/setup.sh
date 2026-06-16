#!/usr/bin/env bash
# Optional explicit setup for drift's Wi-Fi component. drift builds and
# authorizes this on its own on first run, so you normally don't need this —
# it's here to grant access ahead of time or to rebuild. Click "Allow" when
# "drift" asks for your location; after that, drift shows real Wi-Fi names.
set -euo pipefail
cd "$(dirname "$0")"

./build.sh

echo
echo "Launching drift to request Location access…"
echo "→ Click ALLOW on the prompt that appears."
OUT="$(mktemp /tmp/driftwifi.XXXXXX.json)"
open -n ./DriftWiFi.app --args --auth --scan --out "$OUT"

# wait for it to write its result (it reads Wi-Fi after you answer)
for _ in $(seq 1 30); do
    [ -s "$OUT" ] && break
    sleep 1
done

echo
if [ -s "$OUT" ]; then
    echo "Response: $(cat "$OUT")"
    if grep -q '"auth":1' "$OUT"; then
        echo "✅ Authorized — drift will now show real Wi-Fi names."
    else
        echo "⚠️  Not authorized yet. Re-run this script and click Allow, or enable"
        echo "   drift under System Settings → Privacy & Security → Location Services."
    fi
else
    echo "⚠️  No response — the prompt may have been dismissed. Re-run to try again."
fi
rm -f "$OUT"
