#!/bin/bash
# deploy.sh <ziel>, rollt den game-arbiter an einen seiner beiden Orte aus.
# Kanonisch ist dieses Gitea-Repo.
#
#   ./deploy.sh node18    Proxmox-Node .18: Arbiter + Bridge + Timer + MC-Proxyschicht/Compose (LXC 203)
#   ./deploy.sh gamehost   Spiele-VPS: Arbiter + Bridge + Timer, host-natives Profil, KEIN Minecraft
#
# Ohne Argument: node18 (das war jahrelang das einzige Ziel).
#
# Ausgefuehrt wird das Skript dort, wo die Schluessel liegen: node18 braucht ~/.ssh/id_node1
# (host oder Laptop), gamehost den SSH-Alias 'gamehost' (Laptop). Das Repo bleibt kanonisch
# auf host -- ausgerollt wird von Hand, nicht vom Host aus.
#
#   --timer   den 60-s-Tick gleich scharf stellen. OHNE dieses Flag wird der Timer NICHT
#             angefasst -- beim ersten Ausrollen auf einen Host, auf dem schon Spiele laufen,
#             will man erst 'arbiter.py --adopt --live' machen und einen Probe-Tick lesen.
set -euo pipefail
ZIEL="${1:-node18}"
[ "${ZIEL#-}" != "$ZIEL" ] && ZIEL=node18          # erstes Argument war ein Flag
TIMER_SCHARF=0
for a in "$@"; do [ "$a" = "--timer" ] && TIMER_SCHARF=1; done
HERE="$(cd "$(dirname "$0")" && pwd)"

case "$ZIEL" in
  node18)
    SSH="ssh -i $HOME/.ssh/id_node1 -o IdentitiesOnly=yes -o StrictHostKeyChecking=no root@192.0.2.10"
    REGISTRY="$HERE/arbiter/games.json"; PROFIL=""; BRIDGE_ENV="" ;;
  gamehost)
    SSH="ssh gamehost"
    REGISTRY="$HERE/arbiter/games.gamehost.json"
    PROFIL="$HERE/arbiter/arbiter.gamehost.json"
    BRIDGE_ENV="$HERE/arbiter/wake-bridge.gamehost.env" ;;
  *) echo "unbekanntes Ziel '$ZIEL' (node18 | gamehost)"; exit 2 ;;
esac
echo "== Ziel: $ZIEL =="

echo "== 1. Arbiter =="
$SSH "mkdir -p /opt/game-arbiter"
$SSH "cat > /opt/game-arbiter/arbiter.py"       < "$HERE/arbiter/arbiter.py"
$SSH "cat > /opt/game-arbiter/arbiter-tests.sh" < "$HERE/tests/arbiter-tests.sh"
$SSH "mkdir -p /opt/game-arbiter/tests"
$SSH "cat > /opt/game-arbiter/tests/test_timers.py"     < "$HERE/tests/test_timers.py"
$SSH "cat > /opt/game-arbiter/tests/test_snap_guards.py" < "$HERE/tests/test_snap_guards.py"
$SSH "cat > /opt/game-arbiter/tests/test_update.py"      < "$HERE/tests/test_update.py"
$SSH "chmod +x /opt/game-arbiter/arbiter-tests.sh && python3 -m py_compile /opt/game-arbiter/arbiter.py && echo '  arbiter.py ok'"

# Update-Werkzeug: liegt beim Arbiter, weil es ihn fuer jeden Schritt benutzt (Wartung,
# Snapshot, Start, Stopp). Der Aufruf soll kurz sein, deshalb zusaetzlich ein Name in
# /usr/local/sbin: 'ssh gamehost spiel-aktualisieren valheim --live'.
$SSH "cat > /opt/game-arbiter/spiel-aktualisieren.py" < "$HERE/arbiter/spiel-aktualisieren.py"
$SSH "chmod +x /opt/game-arbiter/spiel-aktualisieren.py && ln -sf /opt/game-arbiter/spiel-aktualisieren.py /usr/local/sbin/spiel-aktualisieren && python3 -m py_compile /opt/game-arbiter/spiel-aktualisieren.py && echo '  spiel-aktualisieren ok'"

# Die Registry ist das einzige Stueck, das am Host bewusst abweichen darf: wer ein Spiel
# hinzufuegt oder abzieht, tut das oft direkt dort. Sie blind zu ueberschreiben hat am
# 21.08. schon einmal einen neueren Live-Stand gekostet -- deshalb hier ein Vergleich
# statt eines stillen 'cat >'.
echo "== 1a. Registry ($(basename "$REGISTRY")) =="
if $SSH "test -f /opt/game-arbiter/games.json" 2>/dev/null; then
  if $SSH "cat /opt/game-arbiter/games.json" < /dev/null | diff -q - "$REGISTRY" >/dev/null 2>&1; then
    echo "  Registry identisch -- nichts zu tun"
  elif [ "${FORCE_REGISTRY:-0}" = "1" ]; then
    $SSH "cat > /opt/game-arbiter/games.json" < "$REGISTRY"
    echo "  Registry ueberschrieben (FORCE_REGISTRY=1)"
  else
    echo "  !! Die Live-Registry weicht vom Repo ab. Sie wurde NICHT angefasst."
    echo "     Unterschiede ansehen:  $SSH 'cat /opt/game-arbiter/games.json' | diff - $REGISTRY"
    echo "     Absicht?  Live-Stand ins Repo holen -- oder mit FORCE_REGISTRY=1 ueberschreiben."
  fi
else
  $SSH "cat > /opt/game-arbiter/games.json" < "$REGISTRY"
  echo "  Registry neu angelegt"
fi
[ -n "$PROFIL" ] && { $SSH "cat > /opt/game-arbiter/arbiter.json" < "$PROFIL"; echo "  Host-Profil gesetzt (roles: kein Minecraft, kein Lab)"; }

echo "== 1b. wake-bridge (HTTP-Trigger + /ready-Readiness fuer den Discord-Ladebalken) =="
$SSH "cat > /opt/game-arbiter/wake-bridge.py"           < "$HERE/arbiter/wake-bridge.py"
$SSH "cat > /etc/systemd/system/wake-bridge.service"    < "$HERE/arbiter/wake-bridge.service"
[ -n "$BRIDGE_ENV" ] && $SSH "cat > /etc/default/wake-bridge" < "$BRIDGE_ENV"
$SSH "python3 -m py_compile /opt/game-arbiter/wake-bridge.py && echo '  wake-bridge.py ok'"
$SSH "test -s /opt/game-arbiter/wake.token || { openssl rand -hex 32 > /opt/game-arbiter/wake.token; chmod 600 /opt/game-arbiter/wake.token; echo '  wake.token neu erzeugt'; }"
$SSH "systemctl daemon-reload && systemctl enable wake-bridge.service >/dev/null 2>&1 && systemctl restart wake-bridge.service && echo '  wake-bridge.service enabled + (re)started'"

echo "== 1c. Arbiter-Tick (Timer alle 60s: Idle-Stop + Selbstheilung) =="
# Lagen bis 2026-08-20 nur live auf .18 und in keinem Rezept: dadurch war der
# Timer, der das gesamte Auto-Off traegt, bei einem Node-Neuaufbau verloren.
$SSH "cat > /etc/systemd/system/game-arbiter.service" < "$HERE/arbiter/game-arbiter.service"
$SSH "cat > /etc/systemd/system/game-arbiter.timer"   < "$HERE/arbiter/game-arbiter.timer"
$SSH "systemctl daemon-reload"
if [ "$TIMER_SCHARF" = "1" ]; then
  $SSH "systemctl enable --now game-arbiter.timer >/dev/null 2>&1 && echo '  game-arbiter.timer enabled + gestartet'"
else
  $SSH "systemctl is-enabled game-arbiter.timer 2>/dev/null | grep -q enabled && echo '  Timer war schon scharf, bleibt es' || echo '  Timer NICHT scharf gestellt (--timer, wenn gewollt)'"
fi

if [ "$ZIEL" = "gamehost" ]; then
  echo
  echo "== fertig ($ZIEL). Bevor der Timer scharf geht: =="
  echo "   ssh gamehost 'python3 /opt/game-arbiter/arbiter.py --adopt --live'   # Laufendes uebernehmen"
  echo "   ssh gamehost 'python3 /opt/game-arbiter/arbiter.py'                  # Probe-Tick (dry) lesen"
  echo "   ./deploy.sh gamehost --timer                                         # dann erst scharf"
  exit 0
fi

# ── Minecraft: nur dort ausrollen, wo es die Rolle GIBT ────────────────────────
# Der Host sagt in seiner arbiter.json selbst, welche Rollen er haelt. Bis zum
# 2026-09-11 fragte dieses Skript nicht danach und rollte den Minecraft-Teil
# bedingungslos aus. Auf .18 steht dort seit dem Umzug `minecraft: false`, LXC 203
# ist geloescht. Ein Lauf haette also erst den Wecker wieder scharf gestellt und
# waere dann in Abschnitt 2 an `pct exec 203` gescheitert. Genau dieser Wecker lief
# danach 19 Tage leer weiter und verbrauchte dabei 77 Stunden CPU, ohne dass etwas
# rot war: `Active: running`, alle 5 s ein vergeblicher `pct exec` in einen Container,
# den es nicht mehr gibt.
MC_ROLLE=$($SSH "python3 -c \"
import json
try:
    print(json.load(open('/opt/game-arbiter/arbiter.json'))['roles'].get('minecraft', True))
except FileNotFoundError:
    print(True)      # ohne Profil gilt das alte .18-Verhalten: alle Rollen an
except Exception:
    print('unklar')
\"" 2>/dev/null || echo unklar)

if [ "$MC_ROLLE" = "unklar" ]; then
  echo "!! arbiter.json des Ziels nicht lesbar. Der Minecraft-Teil wird uebersprungen,"
  echo "   statt ihn auf gut Glueck auszurollen. Profil pruefen und erneut ausfuehren."
  exit 3
fi

if [ "$MC_ROLLE" != "True" ]; then
  echo "== Minecraft: dieser Host haelt die Rolle nicht (arbiter.json roles.minecraft=false) =="
  # Nicht nur ueberspringen, sondern einen Rest aus frueheren Laeufen auch abraeumen.
  # Ein stehengelassener Wecker ist teurer als ein fehlender: er sieht gesund aus.
  $SSH "systemctl list-unit-files mc-wake-on-join.service >/dev/null 2>&1 && \
        systemctl is-enabled mc-wake-on-join.service >/dev/null 2>&1 && \
        { systemctl disable --now mc-wake-on-join.service >/dev/null 2>&1; \
          echo '  alter mc-wake-on-join abgeschaltet (haelt hier nichts mehr)'; } || \
        echo '  kein mc-wake-on-join aktiv (richtig)'"
  echo
  echo "== fertig ($ZIEL, ohne Minecraft) =="
  exit 0
fi

echo "== 1d. Wake-on-Join-Daemon (start-on-join fuer MC: folgt Gate-Logs -> wake-bridge) =="
$SSH "cat > /opt/game-arbiter/mc-wake-on-join.py"          < "$HERE/arbiter/mc-wake-on-join.py"
$SSH "python3 -m py_compile /opt/game-arbiter/mc-wake-on-join.py && echo '  mc-wake-on-join.py ok'"
$SSH "cat > /etc/systemd/system/mc-wake-on-join.service" < "$HERE/arbiter/mc-wake-on-join.service"
$SSH "systemctl daemon-reload && systemctl enable mc-wake-on-join.service >/dev/null 2>&1 && systemctl restart mc-wake-on-join.service && echo '  mc-wake-on-join.service enabled + (re)started'"

echo "== 2. Proxyschicht + Paper-Compose (in LXC 203 unter /opt/mc) =="
CTID=203
# AKTIV seit 2026-08-10 ist Weg B: Velocity 4 (haelt den Connect-Tunnel) + NanoLimbo (Warteraum,
# haelt den Spieler verbunden waehrend das Paper-Backend bootet) vor dem Backend.
# ACHTUNG: Dieses Skript rollte bis 2026-08-20 noch docker-compose.gate.yml (Gate Lite) aus und
# haette damit die laufende Proxyschicht ueberschrieben - also den oeffentlichen Zugang gekappt.
# Wer hier etwas aendert: die AKTIVE Variante ist docker-compose.velocity.yml.
$SSH "pct exec $CTID -- mkdir -p /opt/mc/data /opt/mc/velocity /opt/mc/nanolimbo"
# RCON-pw erhalten oder neu erzeugen, dann in die compose injizieren (Platzhalter -> echt)
$SSH "pct exec $CTID -- bash -c '
  test -f /opt/mc/.rcon || { printf \"RCONPW=%s\n\" \"\$(openssl rand -hex 12)\" > /opt/mc/.rcon; chmod 600 /opt/mc/.rcon; }
  . /opt/mc/.rcon'"
# NIE ueberschrieben, weil zur Laufzeit erzeugt und geteilt: forwarding.secret (Velocity<->Limbo<->Paper),
# gate/connect.json (Minekube-Token), .rcon, die Jars. Nur die Quell-Configs gehen aus dem Repo raus.
$SSH "cat > /tmp/velocity.toml" < "$HERE/mc-server/velocity/velocity.toml"
$SSH "pct push $CTID /tmp/velocity.toml /opt/mc/velocity/velocity.toml"
$SSH "cat > /tmp/nanolimbo-settings.yml" < "$HERE/mc-server/nanolimbo/settings.yml"
$SSH "pct push $CTID /tmp/nanolimbo-settings.yml /opt/mc/nanolimbo/settings.yml"
$SSH "cat > /tmp/mc-compose.yml" < "$HERE/mc-server/docker-compose.velocity.yml"
$SSH "pct push $CTID /tmp/mc-compose.yml /opt/mc/docker-compose.yml"
$SSH "pct exec $CTID -- bash -c '. /opt/mc/.rcon; sed -i \"s/__RCONPW__/\$RCONPW/\" /opt/mc/docker-compose.yml'"
echo "  Velocity+NanoLimbo-Config + compose deployt (RCON-pw injiziert; forwarding.secret/connect.json unangetastet)"
echo "  Wirksam wird die Proxyschicht erst beim naechsten Neustart:"
echo "    pct exec $CTID -- bash -c 'cd /opt/mc && docker compose up -d velocity nanolimbo'"

echo "== fertig. Start via Arbiter:  ssh …root@192.0.2.10  python3 /opt/game-arbiter/arbiter.py --live --start-mc =="
