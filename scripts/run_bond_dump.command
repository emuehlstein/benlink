#!/bin/zsh
# Launch the N76 bond+dump from a FOREGROUND Terminal.app session.
#
# macOS attributes Bluetooth pairing prompts and per-process BLE grants to the
# owning application. When this runs under the OpenClaw gateway (a background
# daemon with no UI session) the pairing dialog never surfaces. Running it here
# gives the prompt a real app to attach to.
cd "$(dirname "$0")/.." || exit 1

echo "=============================================="
echo " VR-N76 bond + read-only dump"
echo "=============================================="
echo
echo " 1. Put the radio in pairing mode NOW."
echo " 2. Confirm the radio shows pairing/discoverable."
echo " 3. THEN press return here -- not before."
echo
echo " Pairing mode times out in ~30-60s, so starting the"
echo " scan before it is active is what has been failing."
echo
printf " Radio in pairing mode? press return: "
read -r _
echo "----------------------------------------------"

mkdir -p "$HOME/src/n76/logs"
LOG="$HOME/src/n76/logs/bond_dump-$(date +%Y%m%d-%H%M%S).log"

.venv/bin/python scripts/ble_bond_then_dump.py 2>&1 | tee "$LOG"

echo
echo "----------------------------------------------"
echo "log: $LOG"
echo "Press return to close."
read -r _
