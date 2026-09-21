#!/bin/bash
# farmwelt-reset.sh, setzt die Multiverse-Ressourcenwelt 'farmwelt' zurueck (neuer Seed).
# Grief-Schutz-Design (Owner-Modell): die Farmwelt regeneriert periodisch -> Griefing dort ist
# folgenlos, weil regelmaessig frisch generiert. Spieler in farmwelt werden vorher nach 'hub' teleportiert.
# Laeuft auf Proxmox-Node .18; RCON via 'pct exec' in LXC 203. Deploy-Ziel: /opt/game-arbiter/farmwelt-reset.sh
# Zeitplan: systemd farmwelt-reset.timer (woechentlich Mo 05:00), Owner kann Frequenz anpassen.
set -u
CTID=203; CT=mc-poc
rcon(){ pct exec "$CTID" -- docker exec "$CT" rcon-cli "$@"; }
log(){ echo "$(date '+%F %T') | farmwelt-reset | $*"; }

state=$(pct exec "$CTID" -- docker inspect -f '{{.State.Status}}' "$CT" 2>/dev/null || echo absent)
if [ "$state" != running ]; then log "mc-poc nicht running ($state) -> Reset uebersprungen"; exit 0; fi

log "Start"
rcon say "§eFarmwelt wird jetzt zurueckgesetzt - ihr werdet zum Hub teleportiert." >/dev/null 2>&1
sleep 3
out=$(rcon mv regen farmwelt --seed --remove-players hub 2>&1 | tr -d '\r')
log "mv regen farmwelt --seed --remove-players hub -> ${out:0:200}"
sleep 2
rcon say "§aFarmwelt wurde frisch generiert. Viel Spass!" >/dev/null 2>&1
log "Fertig"
