#!/bin/zsh
# Bond the N76 and run the Tier 3.1 SET_REGION probe in ONE pairing window.
#
# Must run from a foreground Terminal.app session: macOS grants Bluetooth
# per-process and only draws pairing prompts for GUI apps, so this cannot be
# driven from the OpenClaw gateway daemon.
cd "$(dirname "$0")/.." || exit 1

echo "=============================================================="
echo " N76 SET_REGION probe  (khusmann/benlink PR #28 open question)"
echo "=============================================================="
echo
echo " What this does to the radio:"
echo "   - switches region once, snapshots all 32 channels"
echo "   - switches BACK to the original region"
echo "   - verifies zero drift vs the baseline snapshot"
echo " Reversible. No channel data is written."
echo
echo " 1. Put the radio in pairing mode NOW."
echo " 2. Confirm it shows pairing/discoverable."
echo " 3. THEN press return -- not before."
echo
printf " Radio in pairing mode? press return: "
read -r _
echo "--------------------------------------------------------------"

mkdir -p "$HOME/src/n76/logs"
LOG="$HOME/src/n76/logs/set_region_probe-$(date +%Y%m%d-%H%M%S).log"

.venv/bin/python scripts/run_probe_bonded.py 2>&1 | tee "$LOG"

echo
echo "--------------------------------------------------------------"
echo "log: $LOG"
echo "Press return to close."
read -r _
