#!/usr/bin/env python3
"""
game-arbiter (Dateiname arbiter.py aus Timer-Kompatibilitaet): Ressourcen-Prioritaets-
Controller fuer die on-demand-Spielserver.

Er laeuft seit 2026-08-22 an ZWEI Orten, gesteuert von /opt/game-arbiter/arbiter.json:
  * Proxmox-Node .18 (hypervisor3): Spiele in LXCs (kind lxc-systemd/lxc-docker), dazu Minecraft
    und das Windows-AD-Lab. Ohne Profil-Datei gilt genau dieses Verhalten: unveraendert.
  * Spiele-VPS: Spiele host-nativ als systemd-Dienste (kind systemd), kein pct/qm, kein
    Minecraft, kein Lab. roles.minecraft/roles.lab stehen dort auf false.

Prioritaet:  BASIS (xtts/gateway/app-offload, unantastbar)
           > belegte Rolle (Game ODER Minecraft mit >0 Spielern, ODER reserviertes Win-Lab)
           > reservierte, aber leere Rolle
           > P3 unterbrechbare Worker (CI/restic/OCR/Chunky): advisory, nicht aktiv verwaltet

On-Demand-Modell (seit 2026-08-07: MC ist KEINE Sonderrolle mehr): Alle schweren Rollen
(Minecraft + die Spiele der Registry + Win-Lab) sind ON-DEMAND und gleichrangig. NICHTS laeuft
standardmaessig; der Wirt ist idle, wenn niemand spielt.

MEHRERE SPIELE GLEICHZEITIG (seit 2026-08-22): Die Reservierung ist keine einzelne Rolle mehr,
sondern eine Menge: st["games"] haelt je Spiel eigene Uhren. Ob ein weiteres Spiel starten darf,
entscheidet der freie Speicher (min_free_mb je Spiel), nicht mehr die Exklusivitaet. Auf .18
kommt dabei praktisch dasselbe heraus wie vorher, weil dort ohnehin nur eines hineinpasst; auf
dem Spiele-VPS laufen vier nebeneinander. Ein Spiel MIT Spielern wird nie abgeraeumt: laeuft es
ohne Reservierung, uebernimmt der Tick es, statt es zu stoppen. st["reservation"] traegt weiter
'lab' und 'minecraft', die beiden bleiben exklusiv. Minecraft wird, wie jedes Game, per Reservation geweckt (reservation=='minecraft',
via --wake minecraft / --start-mc / wake-bridge /mc/start bzw. /wake/minecraft) und geht nach
idle_timeout_s leer wieder aus (echte Spielerzahl via rcon list). Frueheres 'MC immer-online, wenn
kein Lab' ist ENTFERNT. Gate (Lite) haelt den oeffentlichen Connect-Tunnel DAUERHAFT und ist NICHT
vom Orchestrator gesteuert; ist MC aus, sieht der Spieler offline -> /minecraft Start weckt es.
Ein Gehirn = dieser Orchestrator steuert ausschliesslich mc-poc (docker compose ... mc), nie Gate.

Zwei Auto-Off-Uhren je Rolle (beide brauchen eine Probe, die echte Spielerzahlen liefert):
  * idle_timeout_s: war jemand da und ist wieder weg -> nach N s leer aus.
  * unused_timeout_s: geweckt, joinbar, aber NIE betreten -> nach N s aus ('geweckt und vergessen').
    Der Zaehler startet erst, wenn der Server antwortet, die Bootzeit zaehlt also nie mit.
    idle_timeout_s<=0 (bewusst immer-online) schaltet auch diese Uhr ab.

Yield-Politik (tick):
  * minecraft reserviert   -> MC wach (on-demand), Idle-Auto-Off nach idle_timeout_s leer
  * minecraft NICHT reserviert (Game/Lab reserviert ODER Node idle) -> MC schlaeft (weicht)
  * MC-Crash-Recovery/FAILED-Latch bleibt (einziger Rest-Vorzug), nur aktiv, wenn MC reserviert laeuft
  * Win-Lab: reserviert -> geschuetzt; unreserviert + keine Sitzung >45min -> graceful Idle-Auto-Off

Start-Prio: Reicht der freie Speicher, startet ein Spiel, ohne dass irgendjemand weicht. Reicht
er nicht, weichen zuerst LEERE Rollen (leere Spiele: laengste Leerzeit zuerst, dann schlafendes MC,
unreserviertes Lab). Bleibt es zu eng, entscheidet blocking_role: eine BELEGTE Rolle (Game/MC mit
>0 Spielern) hat Vorrang, der Start wird ABGELEHNT (--wake: exit 3 + Log-Zeile fuer Discord).
Ein RESERVIERTES Win-Lab hat unabhaengig vom Speicher Vorrang, das ist eine ausdrueckliche
Owner-Ansage.

Wer spielt, wird NICHT gekickt, es gibt keinen Override (Owner-Ansage 2026-08-23, das fruehere
--force ist ersatzlos entfallen). Wem der Platz fehlt, der bekommt eine Absage und versucht es
spaeter erneut; ein leeres Spiel raeumt dagegen von selbst den Weg. Eine RESERVIERUNG
(--reserve-game / --reserve-lab, im Lab 'Wartungsmodus') schuetzt eine Rolle auch dann, wenn
gerade niemand darauf ist.

Sicherheit: DRY-RUN default (ohne --live nur protokollieren). VM-Aktionen graceful via
'qm shutdown'; harter 'qm stop' NUR bei manuellem --evict-lab mit --confirm-hard-evict.
flock, append-only audit.log, atomare state.json + rich Live-Snapshot fuer MQTT-Panel (P4).

Kommandos: --start-mc/--stop-mc (= --wake/--sleep minecraft) --start-lab [--reserve] --stop-lab
           --reserve-lab --release  --wake/--start-game <g> --sleep/--stop-game <g>
           --reservieren <g> / --freigeben <g>  (Schutz vor Auto-Off UND Verdraengung)
           --wartung-an <g> [--grund "..."] / --wartung-aus <g>  (Spiel startet nicht,
               solange jemand daran arbeitet; jeder Weckweg bekommt eine Absage im Klartext)
           --reserve-game <g> (etwas anderes: traegt <g> in den Slot -> Start beim naechsten Tick)
           --restart-game <g> --list-games   (<g> inkl. 'minecraft' -> gleiche on-demand-Rolle)
           --snapshot <g> [--world <id>] --snapshot-all --list-snapshots <g>
           --restore <g> <datei> --delete-snapshot <g> <datei> --delete-world <g> <id>
           --adopt (laufende Spiele uebernehmen, ohne etwas zu starten/stoppen)
           --evict-lab --reset --status --test-precheck --tick(default)
Verdraengung (manuell): --evict-lab --confirm-evict [--confirm-hard-evict]
Test-Schalter: --simulate-lab  --min-free N
"""
import json, os, re, subprocess, sys, time, fcntl
from datetime import datetime

CFG = {
    "mc_ctid": 203, "mc_container": "mc-poc",
    # Always-on-Proxyschicht (Weg B, 2026-08-10): Velocity 4 (haelt Connect-Tunnel greenleaf) +
    # NanoLimbo (Warteraum, haelt den Spieler verbunden waehrend das Backend bootet). Loest Gate Lite ab.
    # GATE = Velocity = der tunnel-haltende Dienst; LIMBO = NanoLimbo. Beide werden nie vom Arbiter gestoppt.
    "gate_container": "mc-velocity", "limbo_container": "mc-nanolimbo",
    "lab_vmids": [210, 211],
    "min_free_mb_for_mc": 2600,   # MC-Backend-Bedarf ~2.6G; Precheck fuers Wecken
    "lab_idle_timeout_s": 2700,   # unreserviertes Lab: graceful aus nach 45min OHNE aktive Sitzung
                                  # (idle-basiert via 'query session', NICHT mehr fix seit Start)
    "idle_timeout_s": 1200,       # MC on-demand: 20min leer NACH Nutzung -> auto-off (echte
                                  # Spielerzahl via rcon list). 0 = nie auto-off (immer-online).
    "unused_timeout_s": 900,      # MC geweckt, aber NIE betreten -> nach 15min joinbar+leer auto-off.
                                  # Schliesst das Leck 'geweckt und vergessen' (s. DEFAULT_UNUSED_TIMEOUT_S).
    "max_restarts": 3,
    # DayZ + weitere on-demand Games: jetzt DEKLARATIV in games.json (nicht mehr hier hardcoded).
}
# Grace-Timeout fuer eine geweckte, aber NIE betretene Rolle. Ohne ihn lief ein Server, den
# jemand weckte und dann doch nicht betrat, unbegrenzt weiter: der Auto-Off-Timer griff erst
# NACH der ersten Nutzung (game_was_used). Im Audit-Log der ersten drei Wochen summierten sich
# so 63,6 h Leerlauf ueber 47 Phasen, die laengste 38 h am Stueck (dayz). Der Zaehler laeuft erst,
# wenn der Server wirklich joinbar ist (Probe antwortet) -> die Bootzeit zaehlt nie mit.
# Je Game via games.json "unused_timeout_s" ueberschreibbar; 0 schaltet ihn ab.
DEFAULT_UNUSED_TIMEOUT_S = 900
# Wie lange der angemeldete Bedarf eines frisch gestarteten Spiels mitzaehlt, bevor er
# im gemessenen freien Speicher sichtbar wird (s. booting_reserve_mb).
BOOT_RESERVE_S = 180

# Frist, nach der ein Startversuch als gescheitert gilt. 'systemctl start' quittiert bei
# Type=simple sofort mit rc=0 -- der Rueckgabewert sagt also nur, dass der Auftrag
# angenommen wurde, nicht dass der Server laeuft. Die Quittung wird deshalb im naechsten
# Tick eingeholt: steht die Unit dann weder 'active' noch 'activating', hat der Start
# nicht gehalten. 90 s liegt sicher jenseits von systemds StartLimit-Fenster (5 Versuche
# in 10 s) und unter zwei Tick-Abstaenden, faellt also nie zwischen zwei Durchlaeufe.
START_QUITTUNG_S = 90
def backoff_s(fehler):
    """Abstand bis zum naechsten Startversuch nach <fehler> Fehlstarts in Folge.

    Die ersten beiden Versuche laufen ohne Zusatzwartezeit -- die haeufigste Ursache ist
    ein Port, den der eben gestoppte Vorgaenger noch haelt, und die ist nach einer Minute
    weg. Danach verdoppelt sich der Abstand bis zu einer halben Stunde. Aufgegeben wird
    NIE: ein Spiel, dessen Ursache jemand behebt, soll von selbst zurueckkommen, ohne dass
    ein Mensch daran denken muss."""
    return 0 if fehler < 3 else min(1800, 60 * 2 ** (fehler - 3))

# Absage-Texte. Sie stehen hier als Konstanten, weil sie unveraendert bis zum Menschen
# durchgereicht werden: Arbiter -> audit.log/exit 3 -> wake-bridge -> Dashboard bzw.
# Discord. Frueher endete jede Absage mit dem Hinweis, ein Superadmin koenne sie per
# --force uebergehen; seit dem 2026-08-23 gibt es diesen Weg nicht mehr (Owner-Ansage:
# wer spielt, wird nicht gekickt). Eine Absage ist deshalb endgueltig und muss dem
# Anfragenden sagen, was er stattdessen tun kann, sonst sucht er nach dem Schalter.
ABSAGE_BELEGT   = "dort wird gerade gespielt und niemand wird aus dem Spiel geworfen."
ABSAGE_KEIN_RAM = "der Arbeitsspeicher des Wirts reicht dafuer gerade nicht."

# Welche Rueckgaben von cmd_start_game gelten als Erfolg. ★ Bewusst eine POSITIV-Liste:
# bis zum 2026-08-27 stand hier die Umkehrung (`3 if r in ("rejected","bad-world") else 0`),
# und weil "no-ram" spaeter dazukam, ohne in dieser Liste zu landen, meldete eine
# Speicher-Absage rc=0, also Erfolg. Der Spieler sah im Discord einen Ladebalken fuer
# einen Server, der nie startete. Dasselbe galt fuer "unknown" (vertippter Spielname).
# Mit einer Positiv-Liste faellt jede kuenftige Rueckgabe auf die Fehlerseite, statt still
# als Erfolg durchzurutschen.
START_ERFOLG = ("started", "already-running")
ABSAGE_NACHSATZ = "Bitte spaeter erneut versuchen: sobald dort niemand mehr spielt, geht es von selbst."

# Audit-Log: Der Tick schrieb bisher JEDE Minute eine Lagezeile, auch wenn sich nichts aenderte
# (73 % des Logs waren identische Wiederholungen, 5,8 MB ohne Rotation). Wiederkehrende
# Lagemeldungen laufen deshalb ueber audit_status() -> nur bei Aenderung, sonst hoechstens
# alle LOG_HEARTBEAT_S ein Lebenszeichen. Aktionen/Fehler/Transitions gehen weiter ungefiltert
# durch audit(). Rotation haelt die Datei selbst in Schach (kein logrotate-Eintrag noetig).
LOG_HEARTBEAT_S = 1800
LOG_MAX_BYTES = 8 * 1024 * 1024
LOG_KEEP = 2

BASE = "/opt/game-arbiter"
STATE_FILE, AUDIT_LOG, LOCK_FILE = BASE + "/state.json", BASE + "/audit.log", BASE + "/arbiter.lock"
STATUS_FILE = BASE + "/status.json"   # rich Live-Snapshot (P4: MQTT/Panel)

# Spiel-Zustand als Prometheus-Metriken. Geschrieben wird NUR, wenn dieses Verzeichnis
# schon existiert -- es gehoert dem node-exporter. Damit schaltet sich der Export dort
# von selbst ein, wo einer laeuft (Spiele-VPS), und bleibt auf Node .18 ohne eine Zeile
# Konfiguration still. Der Weg ist bewusst der schon vorhandene: der node-exporter wird
# ueber den bestehenden SSH-Tunnel abgeholt, es braucht keinen Dienst, keinen Port und
# keine Firewall-Lockerung. Gegenstueck: MQTT (publish_mqtt) -- das setzt eine Route ins
# Heimnetz voraus, die es vom oeffentlichen VPS aus nicht gibt.
METRICS_FILE = "/var/lib/prometheus/node-exporter/spiele.prom"
MC_DIR = "/opt/mc"
CTID, CT, GATE = str(CFG["mc_ctid"]), CFG["mc_container"], CFG["gate_container"]
LIMBO = CFG["limbo_container"]
DRY = True
_SIM_LAB = None

# --- Host-Profil: was es an DIESEM Ort ueberhaupt gibt --------------------------
# Der Arbiter laeuft seit 2026-08-22 an zwei Orten: auf Proxmox-Node .18 (Spiele in LXCs,
# dazu Minecraft und das Windows-Lab) und host-nativ auf dem Spiele-VPS (systemd-Dienste,
# kein pct/qm, kein MC, kein Lab). Ohne Profil-Datei bleibt alles wie auf .18, deshalb
# aendert sich dort durch diesen Umbau nichts.
# mc_gate ist bewusst von minecraft getrennt: die Proxyschicht (Velocity + NanoLimbo) ist
# ALWAYS-ON und der einzige Weckweg, der ohne Discord auskommt. Wird Minecraft irgendwann ein
# normales Registry-Spiel, muss roles.minecraft aus -- die Proxyschicht soll trotzdem bewacht
# bleiben. Ohne eigenen Schalter waere sie in dem Moment stillschweigend unbeaufsichtigt.
_DEFAULT_PROFILE = {"node": "node18", "roles": {"minecraft": True, "mc_gate": None, "lab": True}}
def load_profile():
    p = {"node": _DEFAULT_PROFILE["node"], "roles": dict(_DEFAULT_PROFILE["roles"])}
    try:
        with open(BASE + "/arbiter.json") as fh: raw = json.load(fh)
        if isinstance(raw.get("node"), str) and raw["node"]: p["node"] = raw["node"]
        for k, v in (raw.get("roles") or {}).items(): p["roles"][k] = bool(v)
    except Exception: pass
    return p
PROFILE = load_profile()
def has_role(name):
    wert = PROFILE["roles"].get(name)
    if name == "mc_gate" and wert is None:
        return bool(PROFILE["roles"].get("minecraft", False))   # ohne eigene Angabe: wie bisher
    return bool(wert)

# --- On-Demand-Game-Registry (deklarativ via games.json; Fallback = eingebaut) ---
# Jedes Game ist eine schwere Rolle, die MC+Lab verdraengt (reservation=<name>).
# Neue Games -> games.json-Eintrag, kein Code. Wirkt beim naechsten Tick.
_DEFAULT_GAMES = [{"name": "dayz", "kind": "lxc-systemd", "ctid": 204, "service": "dayz-server",
                   "probe": {"type": "a2s", "ip": "192.0.2.10", "port": 27016},
                   "min_free_mb": 4200, "idle_timeout_s": 0}]
def load_games():
    try:
        with open(BASE + "/games.json") as fh:
            g = json.load(fh).get("games")
            if isinstance(g, list) and g: return g
    except Exception: pass
    return _DEFAULT_GAMES
GAMES = load_games()
def game_names(): return [g["name"] for g in GAMES]
def game_by_name(n):
    for g in GAMES:
        if g.get("name") == n: return g
    return None

# --- Multi-Welten-Registry (Games mit "multi_world": true in games.json) ---
# worlds.json haelt je Game die bekannten Welten + die aktive. Die Welt-DATEIEN
# entstehen lazy beim ersten Start via ensure-world.sh IM Game-LXC (deshalb
# funktioniert Anlegen auch bei gestopptem LXC). Games ohne Eintrag/Flag laufen
# unveraendert single-world (voll rueckwaertskompatibel).
WORLDS_FILE = BASE + "/worlds.json"
WORLD_ID_RE = re.compile(r"^[a-z0-9-]{3,24}$")
MAX_WORLDS_PER_GAME = 6
DEFAULT_WORLD = "greenleaf"

def mc_sonderrolle(name):
    """Soll <name> ueber die EINGEBAUTE Minecraft-Maschine laufen (eigene Health-/Crash-Logik,
    Proxyschicht, mc_stop/mc_start), oder als gewoehnliches Registry-Spiel?

    Ein ausdruecklicher Eintrag in games.json gewinnt IMMER. Das ist der Unterschied zwischen
    'es gibt hier eine Minecraft-Rolle' und 'jemand hat Minecraft als Spiel eingetragen'.
    Ohne diese Unterscheidung landeten --wake/--sleep/--restart/--probe minecraft auch dort in
    der Sonderrolle, wo es sie gar nicht gibt: der Aufruf wurde von der alten Exklusivitaets-
    regel abgelehnt ('anderes Spiel hat Vorrang'), und --probe meldete alles auf false,
    waehrend der Container gesund lief. Der Tick war davon nie betroffen, er liest die Registry.
    """
    return name == "minecraft" and has_role("minecraft") and game_by_name("minecraft") is None

def game_multi_world(name):
    g = game_by_name(name)
    return bool(g and g.get("multi_world"))

def load_worlds():
    try:
        with open(WORLDS_FILE) as fh:
            w = json.load(fh)
            if isinstance(w, dict): return w
    except Exception: pass
    return {}

def save_worlds(w):
    tmp = WORLDS_FILE + ".tmp"
    with open(tmp, "w") as f: json.dump(w, f, indent=2)
    os.replace(tmp, WORLDS_FILE)

def world_info(name):
    """(aktive Welt, [alle Welt-IDs]) fuer multi_world-Games, sonst (None, [])."""
    if not game_multi_world(name): return None, []
    w = load_worlds().get(name) or {}
    ids = [x.get("id") for x in w.get("worlds", []) if x.get("id")]
    if not ids: ids = [DEFAULT_WORLD]
    active = w.get("active") if w.get("active") in ids else ids[0]
    return active, ids

def set_active_world(name, world):
    w = load_worlds()
    entry = w.setdefault(name, {})
    ids = [x.get("id") for x in entry.get("worlds", [])]
    if not ids:
        entry["worlds"] = [{"id": DEFAULT_WORLD, "label": DEFAULT_WORLD.capitalize()}]
        ids = [DEFAULT_WORLD]
    if world not in ids:   # darf nie passieren (vorher validiert), Backstop
        return False
    entry["active"] = world
    save_worlds(w)
    return True

def cmd_create_world(name, wid, label=None):
    """Welt in der Registry anlegen (Dateien entstehen lazy beim ersten Start)."""
    if not game_multi_world(name):
        audit("[create-world] '%s' ist kein Multi-Welten-Game" % name); return "unknown"
    if not WORLD_ID_RE.match(wid or ""):
        audit("[create-world:%s] ungueltige Welt-ID '%s' (a-z0-9-, 3-24)" % (name, wid)); return "invalid"
    w = load_worlds()
    entry = w.setdefault(name, {})
    worlds = entry.setdefault("worlds", [])
    if not worlds:
        worlds.append({"id": DEFAULT_WORLD, "label": DEFAULT_WORLD.capitalize()})
        entry.setdefault("active", DEFAULT_WORLD)
    if any(x.get("id") == wid for x in worlds):
        audit("[create-world:%s] Welt '%s' existiert bereits" % (name, wid)); return "exists"
    if len(worlds) >= MAX_WORLDS_PER_GAME:
        audit("[create-world:%s] ABGELEHNT: Cap %d Welten erreicht" % (name, MAX_WORLDS_PER_GAME)); return "cap"
    worlds.append({"id": wid, "label": (label or wid.capitalize())[:40], "created": now()})
    save_worlds(w)
    audit("[create-world:%s] Welt '%s' registriert (Dateien entstehen beim ersten Start)" % (name, wid))
    return "created"

# --- Welt-Snapshots (/var/backups/game-saves, restic-gedeckt) -----------------
# save-Spec kommt aus games.json (parent/items/world_items); Minecraft hat keinen
# games.json-Eintrag (eigene Rolle) -> Spec hier. Snapshots laufen bei laufendem
# LXC via pct exec, bei gestopptem via pct mount vom Host (kein Boot, keine
# Service-Autostarts): behebt zugleich, dass der alte Nightly-Hook gestoppte
# LXCs uebersprang und die Tars seit dem LXC-Shutdown-Umbau einfroren.
GS_DIR = "/var/backups/game-saves"
MANUAL_DIR = GS_DIR + "/manual"
SNAP_KEEP_MANUAL = 5
MC_SAVE = {"parent": "/opt/mc/data", "items": ["world", "world_nether", "world_the_end"]}

def _save_spec(name):
    if mc_sonderrolle(name): return MC_SAVE
    return (game_by_name(name) or {}).get("save")

def _snap_ctx(name):
    """Der Ort, an dem die Welt-Dateien liegen: als Registry-Eintrag, den _in() lesen kann.
    Minecraft hat keinen eigenen games.json-Eintrag, wohnt aber im LXC 203; deshalb hier ein
    Stellvertreter statt einer zweiten Fallunterscheidung in jeder Snapshot-Funktion."""
    if mc_sonderrolle(name): return {"name": "minecraft", "kind": "lxc-systemd", "ctid": CFG["mc_ctid"]}
    return game_by_name(name)

def _expand_world(pattern, world):
    """{w} = Welt-ID, {W} = ID mit grossem Anfangsbuchstaben (Terraria benennt
    .wld-Dateien so, siehe ensure-world.sh: herbst -> Herbst.wld)."""
    return pattern.replace("{w}", world).replace("{W}", world[:1].upper() + world[1:])

def _snapshot_items(name, world=None):
    spec = _save_spec(name) or {}
    items = [_expand_world(p, world) for p in (spec.get("world_items") or [])] if world else []
    return items + list(spec.get("items") or [])

def _lxc_mount(ctid):
    rc, out, err = run("pct mount %d" % ctid, timeout=30)
    if rc != 0:
        return None
    m = re.search(r"'([^']+)'", out + " " + err)
    return m.group(1) if m else "/var/lib/lxc/%d/rootfs" % ctid

def _lxc_unmount(ctid):
    run("pct unmount %d" % ctid, timeout=30)

# Shell-Fragment wie im alten Nightly-Hook: nur existierende items einsammeln,
# leere Menge = Fehler (nie ein leeres/kaputtes Tar ueber ein gutes schieben).
_COLLECT = 'd=; for x in %s; do [ -e "$x" ] && d="$d $x"; done; [ -n "$d" ] || exit 1'

# Eigener Exit-Code fuer die Welt-Pruefung: klar unterscheidbar von tar-rc 1
# ("file changed as we read it"), das wir bewusst tolerieren.
_CHECK_RC = 9

def _snap_pre_cmd(ctx, spec):
    """save.pre_cmd am Spiel-Ort ausfuehren, bevor getart wird (Terraria: die aktiv geladenen
    Mod-Binaries nach mods-backup/ spiegeln). Bei einem gestoppten LXC entfaellt es, ein
    gestoppter Container kann nichts erzeugen, dort wird der zuletzt erzeugte Stand einfach
    mitgesichert. Fehler sind eine Warnung, kein Abbruch: die Welt zu sichern ist wichtiger
    als ihr Beiwerk."""
    cmd = (spec or {}).get("pre_cmd")
    if not cmd or (game_is_lxc(ctx) and not _lxc_running(ctx["ctid"])):
        return
    rc, out, err = run(_in(ctx, "sh -c '%s'" % cmd), timeout=300)
    if rc != 0:
        audit("[snapshot] pre_cmd '%s' rc=%d: weiter ohne frischen Stand (%s)"
              % (cmd, rc, (err or out or "").splitlines()[-1:] or ""))

def _check_frag(checks, world):
    """sh-Fragment, das die Kern-Dateien VOR dem Tar prueft. Faengt den Fall ab, in dem
    ein abgeschnittener/leerer Weltstand den letzten guten im festen Nightly-Ziel
    ueberschreibt. Meldungen nach stderr: stdout ist der Tar-Strom.
    Geprueft wird gegen das echte Terraria-Format (2026-08-22 an der Live-Welt
    verifiziert): .wld traegt 'relogic' ab Byte 4 und schliesst mit dem Weltnamen ab
    (Terrarias eigene Vollstaendigkeits-Kennung), .twld ist gzip und damit per CRC
    vollstaendig pruefbar."""
    parts = []
    for c in (checks or []):
        p = c.get("path") or ""
        p = _expand_world(p, world) if world else p
        if not p:
            continue
        parts.append('f="%s"' % p)
        parts.append('[ -f "$f" ] || { echo "fehlt: $f" >&2; exit %d; }' % _CHECK_RC)
        if c.get("min_bytes"):
            mb = int(c["min_bytes"])
            parts.append('s=$(stat -c %%s "$f" 2>/dev/null || echo 0); [ "$s" -ge %d ] || '
                         '{ echo "zu klein: $f ($s < %d Byte)" >&2; exit %d; }'
                         % (mb, mb, _CHECK_RC))
        if c.get("magic"):
            mg, off = str(c["magic"]), int(c.get("magic_offset", 0))
            parts.append('dd if="$f" bs=1 skip=%d count=%d 2>/dev/null | grep -qa "%s" || '
                         '{ echo "Format-Kennung fehlt: $f" >&2; exit %d; }'
                         % (off, len(mg), mg, _CHECK_RC))
        if c.get("tail_contains"):
            tc = str(c["tail_contains"])
            tc = _expand_world(tc, world) if world else tc
            parts.append('tail -c %d "$f" | grep -qa "%s" || '
                         '{ echo "Abschluss-Kennung fehlt (abgeschnitten?): $f" >&2; exit %d; }'
                         % (int(c.get("tail_bytes", 64)), tc, _CHECK_RC))
        if c.get("gzip_ok"):
            parts.append('gzip -t "$f" 2>/dev/null || '
                         '{ echo "CRC-Fehler: $f" >&2; exit %d; }' % _CHECK_RC)
    return "; ".join(parts)

def _shrink_ok(tmp, dest, max_pct):
    """Zweite Haelfte desselben Schutzes: ein Nightly-Archiv, das gegenueber dem
    bisherigen stark schrumpft, wird nicht uebernommen. Legitimes Schrumpfen (Mod
    entfernt, Welt geloescht) verlangt dann eine bewusste Handlung."""
    if not max_pct or not os.path.exists(dest):
        return True
    try:
        old, new = os.path.getsize(dest), os.path.getsize(tmp)
    except OSError:
        return True
    if old <= 0 or new >= old * (100 - max_pct) / 100.0:
        return True
    audit("[snapshot] ABGEBROCHEN: neues Archiv %.1f MiB gegen bisher %.1f MiB (mehr als "
          "%d%% geschrumpft): alter Stand bleibt. Wenn gewollt: %s loeschen."
          % (new / 1048576.0, old / 1048576.0, max_pct, dest))
    return False

def _snap_tar(ctx, parent, items, dest, check="", shrink_max_pct=None):
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = dest + ".tmp"
    # Getrennt formatieren: `check` enthaelt selbst %-Zeichen (stat -c %s) und darf
    # nicht durch einen zweiten %-Durchlauf laufen.
    tail = ((check + "; ") if check else "") + (_COLLECT % " ".join(items)) \
           + "; tar czf - $d 2>/dev/null"
    if not game_is_lxc(ctx):
        # Host-nativ: die Welt liegt im Dateisystem des Arbiters selbst, kein Umweg,
        # kein Mount, gleicher Pfad ob der Dienst laeuft oder nicht.
        inner = ("cd %s 2>/dev/null || exit 1; " % parent) + tail
        rc, _, err = run(inner + " > " + tmp, timeout=600)
    elif _lxc_running(ctx["ctid"]):
        inner = ("cd %s 2>/dev/null || exit 1; " % parent) + tail
        rc, _, err = run(_in(ctx, "sh -c '%s'" % inner) + " > " + tmp, timeout=600)
    else:
        root = _lxc_mount(ctx["ctid"])
        if not root:
            return False
        try:
            inner = ("cd %s 2>/dev/null || exit 1; " % (root + parent)) + tail
            rc, _, err = run(inner + " > " + tmp, timeout=600)
        finally:
            _lxc_unmount(ctx["ctid"])
    if rc == _CHECK_RC:
        audit("[snapshot] Welt-Pruefung fehlgeschlagen: %s, alter Stand bleibt unangetastet"
              % ((err or "").strip().splitlines() or ["ohne Angabe"])[-1])
        try: os.remove(tmp)
        except OSError: pass
        return False
    # tar rc=1 = "file changed as we read it" (Server schreibt live) -> Archiv ist
    # trotzdem brauchbar (live-konsistent genug, wie im alten Hook). Der Fall
    # "keine items vorhanden" liefert ebenfalls rc=1, aber ein 0-Byte-tmp ->
    # faellt am Groessen-Check durch. Nur rc>=2 ist ein echter tar-Fehler.
    try:
        if rc in (0, 1) and os.path.getsize(tmp) > 0 and _shrink_ok(tmp, dest, shrink_max_pct):
            os.replace(tmp, dest)
            return True
    except OSError:
        pass
    try: os.remove(tmp)
    except OSError: pass
    return False

def _prune_manual(name, token):
    """Manuelle Snapshots je Spiel+Welt-Prefix auf SNAP_KEEP_MANUAL begrenzen
    (Timestamp im Namen -> lexikographisch = chronologisch)."""
    d = os.path.join(MANUAL_DIR, name)
    try:
        files = sorted(f for f in os.listdir(d)
                       if f.startswith(token + "-") and f.endswith(".tar.gz"))
    except OSError:
        return
    for f in files[:-SNAP_KEEP_MANUAL]:
        try:
            os.remove(os.path.join(d, f))
            audit("[snapshot:%s] Retention: %s entfernt (max. %d je Welt)" % (name, f, SNAP_KEEP_MANUAL))
        except OSError:
            pass

def cmd_snapshot(name, world=None, nightly=False, prefix=""):
    """Welt-Snapshot. rc: 0 ok, 1 Fehler, 5 unbekannt/ungueltig.
    nightly=True -> festes Ziel (GS/<game>[/<welt>].tar.gz, wird ueberschrieben);
    sonst manual/<game>/<welt>-<ts>.tar.gz mit Retention. prefix markiert
    Sonder-Snapshots (prerestore/deleted)."""
    spec = _save_spec(name)
    if not spec:
        audit("[snapshot] kein save-Spec fuer '%s'" % name); return 5
    ctx = _snap_ctx(name)
    if not ctx:
        audit("[snapshot] '%s' steht in keiner Registry" % name); return 5
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    if game_multi_world(name):
        active, ids = world_info(name)
        world = world or active or DEFAULT_WORLD
        if world not in ids:
            audit("[snapshot:%s] unbekannte Welt '%s' (bekannt: %s)" % (name, world, ids)); return 5
        token = world
        items = _snapshot_items(name, world)
        dest = (os.path.join(GS_DIR, name, world + ".tar.gz") if nightly else
                os.path.join(MANUAL_DIR, name, "%s%s-%s.tar.gz" % (prefix and prefix + "-", token, ts)))
    else:
        if world:
            audit("[snapshot:%s] hat keine Multi-Welten" % name); return 5
        token = name
        items = _snapshot_items(name)
        dest = (os.path.join(GS_DIR, name + ".tar.gz") if nightly else
                os.path.join(MANUAL_DIR, name, "%s%s-%s.tar.gz" % (prefix and prefix + "-", token, ts)))
    _snap_pre_cmd(ctx, spec)
    # Schrumpf-Schutz nur beim Nightly-Lauf: nur dort wird ein festes Ziel ueberschrieben.
    # Manuelle Snapshots tragen einen Timestamp im Namen und verdraengen nichts.
    ok = _snap_tar(ctx, spec["parent"], items, dest,
                   check=_check_frag(spec.get("validate"), world),
                   shrink_max_pct=(spec.get("shrink_max_pct", 50) if nightly else None))
    if ok:
        sz = run("du -h '%s' | cut -f1" % dest)[1]
        audit("[snapshot:%s] %s ok (%s)" % (name, os.path.relpath(dest, GS_DIR), sz))
        if not nightly:
            _prune_manual(name, (prefix and prefix + "-") + token)
        return 0
    audit("[snapshot:%s] FEHLGESCHLAGEN (%s), vorheriger Stand (falls vorhanden) bleibt"
          % (name, os.path.relpath(dest, GS_DIR)))
    return 1

def cmd_snapshot_all():
    """Nightly-Lauf (restic-Pre-Hook): alle Games, bei multi_world jede Welt.
    Fehlertolerant (rc immer 0), je Game bleibt der letzte gute Stand liegen."""
    for name in ["minecraft"] + game_names():
        if not _save_spec(name):
            continue
        if game_multi_world(name):
            _, ids = world_info(name)
            any_ok = False
            for wid in ids:
                any_ok = (cmd_snapshot(name, world=wid, nightly=True) == 0) or any_ok
            # Alt-Layout (ein Tar fuer alles) erst raeumen, wenn per-Welt-Tars da sind.
            legacy = os.path.join(GS_DIR, name + ".tar.gz")
            if any_ok and os.path.exists(legacy):
                try:
                    os.remove(legacy)
                    audit("[snapshot:%s] Alt-Tar %s.tar.gz entfernt (per-Welt-Layout aktiv)" % (name, name))
                except OSError:
                    pass
        else:
            cmd_snapshot(name, nightly=True)
    return 0

_TS_SUFFIX_RE = re.compile(r"-\d{8}-\d{6}\.tar\.gz$")

def _world_from_fname(fname, ids):
    """Welt-ID aus einem Snapshot-Dateinamen ableiten (prerestore-/deleted-Prefix
    abstreifen, Timestamp-Suffix abschneiden, gegen bekannte IDs matchen)."""
    base = fname
    for p in ("prerestore-", "deleted-"):
        if base.startswith(p):
            base = base[len(p):]
    base = _TS_SUFFIX_RE.sub("", base)
    if base.endswith(".tar.gz"):
        base = base[:-7]
    return base

def _valid_snapfile(name, relfile):
    """Pfad-Validierung fuer restore/delete: nur die von --list-snapshots
    gelisteten Formen, kein Traversal, muss existieren."""
    if not relfile or ".." in relfile or relfile.startswith("/") or not relfile.endswith(".tar.gz"):
        return None
    ok = (relfile == name + ".tar.gz"
          or relfile.startswith(name + "/")
          or relfile.startswith("manual/" + name + "/"))
    if not ok:
        return None
    p = os.path.join(GS_DIR, relfile)
    return p if os.path.isfile(p) else None

def cmd_list_snapshots(name):
    """Read-only: Nightly- + manuelle Snapshots eines Games als JSON."""
    active, ids = world_info(name)
    out = {"game": name, "multi_world": game_multi_world(name), "active": active, "snapshots": []}
    def add(rel, kind, world):
        try:
            stt = os.stat(os.path.join(GS_DIR, rel))
        except OSError:
            return
        out["snapshots"].append({
            "file": rel, "kind": kind, "world": world, "size": stt.st_size,
            "mtime": datetime.fromtimestamp(stt.st_mtime).strftime("%Y-%m-%d %H:%M")})
    if game_multi_world(name):
        d = os.path.join(GS_DIR, name)
        try:
            for f in sorted(os.listdir(d)):
                if f.endswith(".tar.gz"):
                    add(name + "/" + f, "nightly", f[:-7])
        except OSError:
            pass
    else:
        add(name + ".tar.gz", "nightly", name)
    md = os.path.join(MANUAL_DIR, name)
    try:
        for f in sorted(os.listdir(md), reverse=True):
            if f.endswith(".tar.gz"):
                add("manual/" + name + "/" + f, "manual", _world_from_fname(f, ids))
    except OSError:
        pass
    return out

def _game_running(name):
    if mc_sonderrolle(name):
        return mc_ct_state() == "running"
    g = game_by_name(name)
    return bool(g and game_active(g))

def cmd_restore(st, name, relfile):
    """Snapshot zurueckspielen. NUR bei gestopptem, unreserviertem Spiel; legt
    IMMER erst einen prerestore-Sicherheits-Snapshot an. rc: 0 ok, 1 Fehler,
    4 Spiel laeuft/reserviert, 5 Datei/Game unbekannt."""
    spec = _save_spec(name)
    if not spec:
        audit("[restore:%s] kein save-Spec" % name); return 5
    src = _valid_snapfile(name, relfile)
    if not src:
        audit("[restore:%s] ungueltige Datei '%s'" % (name, relfile)); return 5
    if _game_running(name) or game_reserved(st, name) or st.get("reservation") == name:
        audit("[restore:%s] ABGELEHNT: Spiel laeuft oder ist reserviert, erst stoppen" % name); return 4
    ctx = _snap_ctx(name)
    if not ctx:
        audit("[restore:%s] steht in keiner Registry" % name); return 5
    active, ids = world_info(name)
    world = _world_from_fname(os.path.basename(relfile), ids) if game_multi_world(name) else None
    # Sicherheits-Snapshot des aktuellen Stands (best-effort: Welt kann leer sein)
    if game_multi_world(name) and world in ids:
        cmd_snapshot(name, world=world, prefix="prerestore")
    elif not game_multi_world(name):
        cmd_snapshot(name, prefix="prerestore")
    parent = spec["parent"]
    # chown auf den Eigentuemer des parent-Verzeichnisses normalisiert die
    # Ownership unabhaengig davon, in welchem Namespace das Tar entstand
    # (pct exec = Container-UIDs, pct mount = host-shifted UIDs).
    fix = 'chown -R "$(stat -c %u:%g .)" .'
    inner = "cd %s || exit 1; tar xzf - && %s" % (parent, fix)
    if not game_is_lxc(ctx):
        rc, _, err = run("sh -c '%s' < %s" % (inner, src), timeout=600)
    elif _lxc_running(ctx["ctid"]):
        rc, _, err = run(_in(ctx, "sh -c '%s'" % inner) + " < " + src, timeout=600)
    else:
        root = _lxc_mount(ctx["ctid"])
        if not root:
            audit("[restore:%s] pct mount %s fehlgeschlagen" % (name, ctx["ctid"])); return 1
        try:
            rc, _, err = run("cd %s || exit 1; tar xzf '%s' && %s" % (root + parent, src, fix), timeout=600)
        finally:
            _lxc_unmount(ctx["ctid"])
    if rc != 0:
        audit("[restore:%s] FEHLGESCHLAGEN (rc=%s): %s" % (name, rc, (err or "")[:200])); return 1
    # Welt eines geloeschten Snapshots wieder in die Registry aufnehmen -> sofort startbar.
    if world and world not in ids and WORLD_ID_RE.match(world):
        w = load_worlds()
        entry = w.setdefault(name, {})
        entry.setdefault("worlds", []).append({"id": world, "label": world.capitalize(), "created": now()})
        save_worlds(w)
        audit("[restore:%s] Welt '%s' wieder registriert" % (name, world))
    audit("[restore:%s] %s eingespielt (Sicherheits-Snapshot: prerestore-*)" % (name, relfile))
    return 0

def cmd_delete_snapshot(name, relfile):
    """Snapshot-Datei loeschen. rc: 0 ok, 5 unbekannt/ungueltig."""
    src = _valid_snapfile(name, relfile)
    if not src:
        audit("[delete-snapshot:%s] ungueltige Datei '%s'" % (name, relfile)); return 5
    try:
        os.remove(src)
    except OSError as e:
        audit("[delete-snapshot:%s] FEHLGESCHLAGEN: %s" % (name, e)); return 1
    audit("[delete-snapshot:%s] %s geloescht" % (name, relfile))
    return 0

def cmd_delete_world(st, name, wid):
    """Welt komplett entfernen (Registry + Dateien + Nightly-Tar). Vorher ein
    letzter 'deleted-'-Snapshot nach manual/ (Wiederherstellung via --restore
    moeglich). Aktive Welt und letzte Welt sind geschuetzt.
    rc: 0 ok, 4 aktiv/letzte, 5 unbekannt, 1 Fehler."""
    if not game_multi_world(name):
        audit("[delete-world:%s] kein Multi-Welten-Game" % name); return 5
    active, ids = world_info(name)
    if wid not in ids:
        audit("[delete-world:%s] unbekannte Welt '%s' (bekannt: %s)" % (name, wid, ids)); return 5
    if len(ids) <= 1:
        audit("[delete-world:%s] ABGELEHNT: '%s' ist die letzte Welt" % (name, wid)); return 4
    if wid == active:
        audit("[delete-world:%s] ABGELEHNT: '%s' ist die aktive Welt, erst wechseln" % (name, wid)); return 4
    # Abschieds-Snapshot (best-effort: Welt kann nie gestartet worden sein)
    snap_rc = cmd_snapshot(name, world=wid, prefix="deleted")
    if snap_rc == 1:
        audit("[delete-world:%s] kein Abschieds-Snapshot (keine Welt-Dateien?), fahre fort" % name)
    # Welt-Dateien am Spiel-Ort loeschen (nur world_items, nie die geteilten items)
    spec = _save_spec(name) or {}
    items = [_expand_world(p, wid) for p in (spec.get("world_items") or [])]
    if items and spec.get("parent"):
        ctx = _snap_ctx(name) or {}
        rmcmd = "cd %s 2>/dev/null && rm -rf %s" % (spec["parent"], " ".join(items))
        if not ctx:
            audit("[delete-world:%s] steht in keiner Registry, Dateien bleiben liegen" % name)
        elif not game_is_lxc(ctx):
            run("sh -c '%s; true'" % rmcmd, timeout=120)
        elif _lxc_running(ctx["ctid"]):
            run(_in(ctx, "sh -c '%s; true'" % rmcmd), timeout=120)
        else:
            root = _lxc_mount(ctx["ctid"])
            if root:
                try:
                    run("cd %s 2>/dev/null && rm -rf %s; true" % (root + spec["parent"], " ".join(items)), timeout=120)
                finally:
                    _lxc_unmount(ctx["ctid"])
    # Nightly-Tar der Welt entfernen
    try:
        os.remove(os.path.join(GS_DIR, name, wid + ".tar.gz"))
    except OSError:
        pass
    # Registry austragen
    w = load_worlds()
    entry = w.setdefault(name, {})
    entry["worlds"] = [x for x in entry.get("worlds", []) if x.get("id") != wid]
    if entry.get("active") == wid:
        remaining = [x.get("id") for x in entry["worlds"] if x.get("id")]
        entry["active"] = remaining[0] if remaining else DEFAULT_WORLD
    save_worlds(w)
    audit("[delete-world:%s] Welt '%s' entfernt (Abschieds-Snapshot unter manual/, --restore holt sie zurueck)" % (name, wid))
    return 0

def now(): return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
def run(cmd, timeout=30):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
def _rotate_audit():
    """audit.log -> .1 -> .2, wenn es LOG_MAX_BYTES ueberschreitet. Jeder Tick ist ein frischer
    Prozess und oeffnet die Datei neu ('a') -> ein blosses Umbenennen genuegt, kein copytruncate,
    kein offener Deskriptor auf der alten Datei."""
    try:
        if os.path.getsize(AUDIT_LOG) < LOG_MAX_BYTES: return
    except OSError:
        return
    try:
        for i in range(LOG_KEEP - 1, 0, -1):
            src = "%s.%d" % (AUDIT_LOG, i)
            if os.path.exists(src): os.replace(src, "%s.%d" % (AUDIT_LOG, i + 1))
        os.replace(AUDIT_LOG, AUDIT_LOG + ".1")
    except OSError:
        pass

def audit(msg):
    line = now() + " | " + msg
    try:
        _rotate_audit()
        with open(AUDIT_LOG, "a") as f: f.write(line + "\n")
    except OSError as e:
        # Volle/nur-lesbare Platte darf den Controller nie anhalten - Steuern geht vor Buchfuehren.
        print("%s | [warn] audit.log nicht schreibbar: %s" % (now(), e))
    print(line)

def audit_status(st, slot, key, msg):
    """Wiederkehrende Lagemeldung (Tick-Status, Game-Status). Schreibt nur, wenn sich `key`
    gegenueber der zuletzt geschriebenen Meldung dieses Slots geaendert hat - oder wenn seit
    dem letzten Schreiben LOG_HEARTBEAT_S vergangen sind (Lebenszeichen: das Log soll auch bei
    tagelang unveraenderter Lage zeigen, dass der Controller laeuft). Aktionen, Fehler und
    Transitions gehen weiter direkt ueber audit() und werden nie unterdrueckt."""
    last = (st.setdefault("log_last", {}) or {}).get(slot) or {}
    age = time.time() - (last.get("ts") or 0)
    if last.get("key") == key and age < LOG_HEARTBEAT_S:
        print(now() + " | (unveraendert) " + msg)   # journal sieht jeden Tick, das Log nicht
        return
    if last.get("key") == key:
        msg += "  [unveraendert seit %d min]" % int(age / 60)
    st["log_last"][slot] = {"key": key, "ts": time.time()}
    audit(msg)
# Felder frueherer Versionen, die niemand mehr liest. 'want_mc' war besonders irrefuehrend:
# es stand dauerhaft auf true, waehrend MC laengst nur noch per Reservation laeuft - wer den
# Status las, sah 'MC soll laufen', obwohl der Node bewusst idle war.
# game_idle_since/game_unused_since/game_was_used waren die Einzahl-Uhren aus der Zeit, in der
# genau EIN Spiel laufen durfte. Seit dem Mehr-Spiel-Umbau (2026-08-22) fuehrt st["games"] je
# Spiel seine eigenen, die alten Schluessel wandern in der Migration mit und verschwinden dann.
_DEAD_STATE_KEYS = ("want_mc", "autooff_tried", "dayz_was_used", "dayz_idle_since",
                    "game_idle_since", "game_unused_since", "game_was_used")

def _new_state():
    return {"mode": "IDLE", "reservation": "none", "games": {}, "restart_count": 0,
            "idle_since": None, "lab_since": None, "lab_idle_since": None, "mc_idle_since": None,
            "mc_was_used": False, "mc_unused_since": None,
            "log_last": {}, "last_transition": now()}

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f: st = json.load(f)
            st.setdefault("games", {})
            # Migration: frueher hielt der Reservierungs-String das eine erlaubte Spiel. Seine
            # Uhren wandern in den Spiel-Eintrag, damit ein laufender Auto-Off-Timer den Umbau
            # ueberlebt statt von vorn zu beginnen.
            res = st.get("reservation")
            if res and res not in ("none", "lab", "minecraft") and res not in st["games"]:
                st["games"][res] = {"since": None, "idle_since": st.get("game_idle_since"),
                                    "unused_since": st.get("game_unused_since"),
                                    "was_used": bool(st.get("game_was_used")), "world": None}
                st["reservation"] = "none"
            for k in _DEAD_STATE_KEYS: st.pop(k, None)
            return st
        except Exception: pass
    return _new_state()

# --- Reservierungen: eine MENGE von Spielen, nicht mehr ein Slot ----------------
# Auf .18 passte immer nur ein schweres Spiel ins RAM, deshalb war die Reservierung ein
# einzelner String. Auf dem Spiele-VPS laufen mehrere gleichzeitig; die Bremse ist jetzt
# min_free_mb je Spiel, nicht mehr die Exklusivitaet. reservation haelt weiter 'lab' und
# 'minecraft' (beide sind und bleiben exklusive .18-Rollen).
def game_slot(st, name):
    return (st.get("games") or {}).get(name)
def game_reserved(st, name):
    return name in (st.get("games") or {})
def reserve_game(st, name, world=None, was_used=False):
    slot = st.setdefault("games", {}).setdefault(name, {})
    slot.setdefault("since", time.time())
    slot["idle_since"] = None; slot["unused_since"] = None
    slot["was_used"] = bool(slot.get("was_used")) or bool(was_used)
    if world is not None: slot["world"] = world
    return slot
def release_game(st, name):
    (st.get("games") or {}).pop(name, None)
def game_geschuetzt(st, name):
    """Ist dieses Spiel reserviert (= vor Auto-Off UND Verdraengung geschuetzt)?

    Nicht zu verwechseln mit reserve_game()/reserved_games(): die bedeuten 'der Arbiter
    verwaltet diese Rolle gerade' (sie steht im Slot) und sagen nichts darueber aus, ob sie
    geschuetzt ist. Der Schutz ist ein ausdruecklicher Schalter, den ein Admin setzt,
    im Dashboard 'Reserviert', beim Windows-Lab und den Lab-Diensten 'Wartungsmodus'.
    Er ueberlebt einen Neustart des Arbiters, weil er in state.json steht."""
    return bool((game_slot(st, name) or {}).get("geschuetzt"))
def status_schutz_nachziehen(st):
    """Zieht NUR die geschuetzt-Felder in der bestehenden status.json nach.

    status.json schreibt sonst allein der Tick (emit_state). Nach einem
    Reservieren-Kommando haette die Oberflaeche deshalb bis zu 60 s den alten
    Zustand gezeigt und ihren Knopf falsch herum beschriftet, der Nutzer klickt
    dann ein zweites Mal und hebt auf, was er gerade gesetzt hat.

    Bewusst KEIN vollstaendiger Tick an dieser Stelle: der wuerde nebenbei
    Auto-Off und Verdraengung ausfuehren. Ein Schalter darf nichts starten oder
    stoppen. Fehlt die Datei, passiert nichts, der naechste Tick baut sie neu.
    """
    try:
        with open(STATUS_FILE) as f:
            snap = json.load(f)
        detail = ((snap.get("games") or {}).get("detail") or {})
        for n, slot in detail.items():
            slot["geschuetzt"] = game_geschuetzt(st, n)
        tmp = STATUS_FILE + ".tmp"
        with open(tmp, "w") as f: json.dump(snap, f, indent=2)
        os.replace(tmp, STATUS_FILE)
    except Exception:
        pass

def set_game_geschuetzt(st, name, an):
    """Schutz setzen/aufheben. Legt den Slot NICHT an: geschuetzt wird, was laeuft bzw. beim
    naechsten Tick startet, ein Schutz fuer ein gar nicht verwaltetes Spiel waere eine
    stille Karteileiche, die reserved_games() ohnehin herausfiltert."""
    slot = game_slot(st, name)
    if slot is None:
        return False
    if an: slot["geschuetzt"] = True
    else:  slot.pop("geschuetzt", None)
    return True
def reserved_games(st):
    """Reservierte Spiele in Registry-Reihenfolge. Filtert Eintraege heraus, deren Spiel
    aus games.json entfernt wurde, sonst haelt ein Karteileichen-Eintrag ewig RAM-Buchhaltung."""
    return [n for n in game_names() if n in (st.get("games") or {})]
# --- Wartung: dieses Spiel startet gerade bewusst nicht -------------------------
# Gegenstueck zum Schutz oben und bewusst NICHT im Spiel-Slot gefuehrt: den Slot gibt es
# nur, solange der Arbiter eine Rolle verwaltet. Eine Wartung gilt aber gerade fuer das
# SCHLAFENDE Spiel, an dem jemand arbeitet (Update einspielen, Mods tauschen), also genau
# fuer den Fall, in dem kein Slot existiert.
#
# Warum es das braucht: waehrend ein Update laeuft, liegt die Installation halb alt und
# halb neu auf der Platte. Wer in diesem Moment weckt, startet einen Server auf halbem
# Stand. Geweckt wird hier nicht nur von Hand: bei Terraria und Factorio loest schon ein
# Beitrittsversuch am Platzhalter den Weckruf aus, ohne dass ein Mensch beteiligt ist.
# Die Sperre sitzt deshalb in cmd_start_game, wo alle Weckwege zusammenlaufen (Discord,
# Dashboard, Greeter, Tick), und nicht im Update-Werkzeug, das nur seinen eigenen kennt.
def game_in_wartung(st, name):
    """Wartungseintrag des Spiels oder None. Form: {'seit': unixzeit, 'grund': text}."""
    return (st.get("wartung") or {}).get(name)

def set_game_wartung(st, name, an, grund=""):
    """Wartung setzen oder aufheben. Anders als set_game_geschuetzt braucht es keinen Slot:
    ein schlafendes Spiel ist der Normalfall dieser Sperre, nicht ihre Ausnahme."""
    w = st.setdefault("wartung", {})
    if an:
        w[name] = {"seit": time.time(), "grund": (grund or "Wartung").strip()}
    else:
        w.pop(name, None)
    return True

def wartung_games(st):
    """Spiele in Wartung, in Registry-Reihenfolge. Filtert wie reserved_games Eintraege
    heraus, deren Spiel aus games.json verschwunden ist."""
    return [n for n in game_names() if n in (st.get("wartung") or {})]

def wartung_text(w):
    """Wartungseintrag als Klartext fuer Log und Absage. Die Dauer gehoert dazu, weil eine
    Wartung, die seit Stunden laeuft, fast immer eine vergessene ist."""
    dauer = max(0, int(time.time() - (w.get("seit") or time.time())))
    return "%s (seit %d min)" % (w.get("grund") or "Wartung", dauer // 60)

def save_state(st):
    st["last_transition"] = now()
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f: json.dump(st, f, indent=2)
    os.replace(tmp, STATE_FILE)

# ---------- Sensoren (rein lesend) ----------
# Prozess-lokale Caches: `pct list`/`qm list` kosten je ~1s (Proxmox-CLI-Startup). Der Tick
# fragte bisher 5x `pct status` + 2x `qm status` einzeln ab (~7s Overhead). Jeder Tick/Kommando
# ist ein FRISCHER Prozess -> ein einmal gefuellter Cache ist immer aktuell (single-tick), kein
# Stale-Risiko. Post-Aktions-Verifikation (VM start/stop) nutzt bewusst die DIREKTEN Funktionen
# _vm_running()/mc_ct_state(), nicht diese Caches.
_LXC_STATUS = None
_VM_STATUS = None
def _lxc_status():
    global _LXC_STATUS
    if _LXC_STATUS is None:
        _LXC_STATUS = {}
        _, out, _ = run("pct list")
        for line in out.splitlines()[1:]:
            p = line.split()
            if len(p) >= 2 and p[0].isdigit(): _LXC_STATUS[p[0]] = p[1]
    return _LXC_STATUS
def _lxc_running(ctid):
    return _lxc_status().get(str(ctid), "") == "running"
def _vm_status():
    global _VM_STATUS
    if _VM_STATUS is None:
        _VM_STATUS = {}
        _, out, _ = run("qm list")
        for line in out.splitlines()[1:]:
            p = line.split()
            if len(p) >= 3 and p[0].isdigit(): _VM_STATUS[p[0]] = p[2]  # VMID NAME STATUS ...
    return _VM_STATUS
def _dexec(inner):  # docker-Befehl in LXC 203
    return run("pct exec " + CTID + " -- " + inner)
def mc_lxc_running():
    return _lxc_running(CFG["mc_ctid"])
def mc_ct_state():
    rc, out, _ = _dexec("docker inspect -f '{{.State.Status}}' " + CT)
    return out if (rc == 0 and out) else "absent"
def mc_exit_code():
    rc, out, _ = _dexec("docker inspect -f '{{.State.ExitCode}}' " + CT)
    try: return int(out)
    except Exception: return None
def mc_health():
    rc, out, _ = _dexec("docker inspect -f '{{.State.Health.Status}}' " + CT)
    return out if rc == 0 and out else "n/a"
def gate_running():
    # "Proxy up" = Velocity (Tunnel/Connect) UND NanoLimbo (Warteraum) laufen beide -> ein Spieler
    # wird bei schlafendem Backend im Limbo GEHALTEN statt getrennt. Beide sind ALWAYS-ON.
    rc, out, _ = _dexec("docker inspect -f '{{.State.Status}}' " + GATE + " " + LIMBO)
    return rc == 0 and out.split() == ["running", "running"]
def mc_players():
    rc, out, _ = _dexec("docker exec " + CT + " rcon-cli list")
    if rc != 0: return -1
    m = re.search(r"There are (\d+)", out); return int(m.group(1)) if m else -1
def lab_running():
    if _SIM_LAB is not None: return list(_SIM_LAB)
    vs = _vm_status()
    return [vid for vid in CFG["lab_vmids"] if vs.get(str(vid), "") == "running"]
def _vm_running(vid):
    _, out, _ = run("qm status %d" % vid); return "running" in out
def lab_ram_need():
    total = 0
    for vid in CFG["lab_vmids"]:
        _, out, _ = run("qm config %d" % vid)
        m = re.search(r"^memory:\s*(\d+)", out, re.M)
        if m: total += int(m.group(1))
    return total
def free_mb():
    _, out, _ = run("free -m | awk '/Mem:/{print $7}'")
    try: return int(out)
    except Exception: return -1
def lab_session_active():
    """True wenn eine laufende Lab-VM eine ANGEMELDETE Sitzung hat (Active: RDP oder Konsole)
    = 'Lab wird gerade genutzt'. Ueber den Guest-Agent ('query session'). Kein Agent / kein
    Kontakt / Fehler -> False (dann laeuft der Idle-Timer; ein RESERVIERTES Lab ist ohnehin
    auto-off-geschuetzt, deshalb ist ein Fehl-False hier ungefaehrlich). Ein per RDP verbundener
    Nutzer haelt die Sitzung 'Active' (auch beim Zuschauen); getrennt/abgemeldet -> Idle-Timer."""
    for vid in lab_running():
        rc, out, _ = run("qm guest exec %d -- query session" % vid, timeout=12)
        if rc != 0 or not out:
            continue
        try: data = json.loads(out).get("out-data", "")
        except Exception: data = out
        for line in data.splitlines():
            if "Active" in line.split():   # STATE-Spalte == Active -> angemeldete Sitzung
                return True
    return False

# ---------- Aktionen (nur --live) ----------
def act(desc, cmd, timeout=90):
    if DRY:
        audit("[DRY-RUN] WUERDE: " + desc); return True
    audit("[LIVE] " + desc)
    rc, out, err = run(cmd, timeout=timeout)
    if rc != 0: audit("  ! Fehler rc=%d: %s" % (rc, (err or out)[:180]))
    return rc == 0
def ensure_gate():
    """Sorgt fuer die always-on Proxyschicht und gibt zurueck, ob sie oben ist -> der Tick reicht
    das an emit_state durch, statt gate_running() ein zweites Mal zu fragen (jedes 'pct exec'
    kostet ~1,5 s)."""
    if not mc_lxc_running(): return False
    up = gate_running()
    if not up:
        act("Proxyschicht (Velocity-Tunnel + NanoLimbo-Warteraum) ist unten -> starten",
            "pct exec %s -- bash -c 'cd %s && docker compose up -d velocity nanolimbo'" % (CTID, MC_DIR))
    return up
def mc_start():
    if not mc_lxc_running():
        act("LXC 203 starten", "pct start " + CTID)
        if not DRY: time.sleep(3)
    return act("MC-Backend starten (compose up -d mc)",
               "pct exec %s -- bash -c 'cd %s && docker compose up -d mc'" % (CTID, MC_DIR))
def mc_stop():
    if not DRY:
        run("pct exec %s -- docker exec %s rcon-cli save-all flush" % (CTID, CT), timeout=30)
    return act("MC-Backend sauber stoppen (save-all + compose stop mc, stop_grace 60s)",
               "pct exec %s -- bash -c 'cd %s && docker compose stop mc'" % (CTID, MC_DIR))
def evict_lab(lab, confirm_hard):
    """Verdraengt Lab-VMs manuell. Graceful via 'qm shutdown'; scheitert graceful
    (Timeout/kein Guest-Agent), wird NUR mit --confirm-hard-evict hart per 'qm stop'
    beendet, sonst Abbruch. True nur wenn danach ALLE Lab-VMs wirklich weg sind."""
    all_gone = True
    for vid in lab:
        ok = act("Lab-VM %d per 'qm shutdown' (Guest-Agent, sauber, timeout 90s) herunterfahren" % vid,
                 "qm shutdown %d --timeout 90" % vid, timeout=110)
        if not DRY and ok and _vm_running(vid): ok = False
        if ok: continue
        if not confirm_hard:
            audit("[evict] Lab-VM %d graceful gescheitert -> KEIN Hard-Kill (--confirm-hard-evict fehlt) -> abgebrochen" % vid)
            all_gone = False; continue
        hard = act("Lab-VM %d GRACEFUL GESCHEITERT -> HART 'qm stop' (--confirm-hard-evict)" % vid, "qm stop %d" % vid, timeout=60)
        if not DRY and hard and _vm_running(vid): hard = False
        if not hard: audit("  ! Lab-VM %d konnte auch hart nicht gestoppt werden" % vid); all_gone = False
    return all_gone
def stop_lab_graceful(lab):
    """Nur graceful 'qm shutdown' (fuer auto-off / --stop-lab). Kein Hard-Kill.
    True nur wenn danach ALLE weg sind (leere Shells ohne Guest-Agent -> False, bleiben)."""
    all_gone = True
    for vid in lab:
        ok = act("Lab-VM %d graceful herunterfahren (qm shutdown, timeout 90s)" % vid,
                 "qm shutdown %d --timeout 90" % vid, timeout=110)
        if not DRY and ok and _vm_running(vid): ok = False
        if not ok: all_gone = False
    return all_gone

def precheck_mc(avail):
    if avail < 0: return True   # Sensorfehler -> nicht blockieren
    return avail >= CFG["min_free_mb_for_mc"]

# ---------- Generische On-Demand-Game-Rollen ----------
# kind: lxc-systemd | lxc-docker  -> das Spiel steckt in einem Proxmox-LXC (Node .18)
#       systemd                   -> das Spiel laeuft host-nativ neben dem Arbiter (Spiele-VPS)
# _in() ist der EINZIGE Ort, der das unterscheidet. Ohne diese Weiche muesste jede der ueber
# 40 pct-Stellen es einzeln wissen, und eine vergessene liefe auf dem VPS ins Leere, wo es
# gar kein pct gibt (rc=127, aussieht wie 'Spiel aus').
def game_is_lxc(g):
    return str((g or {}).get("kind", "lxc-systemd")).startswith("lxc-")
def _in(g, cmd):
    """<cmd> im Kontext des Spiels ausfuehren: im LXC via pct exec, host-nativ unveraendert."""
    return ("pct exec %s -- " % g["ctid"]) + cmd if game_is_lxc(g) else cmd

def _a2s_players(ip, port):
    """A2S_INFO (Steam-Query) -> Spielerzahl (int), -1 bei Fehler/kein Kontakt."""
    import socket
    s = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.settimeout(5)
        req = b'\xFF\xFF\xFF\xFF\x54Source Engine Query\x00'
        s.sendto(req, (ip, port)); data, _ = s.recvfrom(4096)
        if len(data) >= 5 and data[4] == 0x41:               # Challenge -> erneut mit Token
            s.sendto(req + data[5:9], (ip, port)); data, _ = s.recvfrom(4096)
        if not (len(data) >= 6 and data[4] == 0x49): return -1
        b = data[6:]
        for _ in range(4): b = b[b.index(b'\x00')+1:]        # name/map/folder/game ueberspringen
        return b[2]                                          # nach app_id-short: players-byte
    except Exception:
        return -1
    finally:
        if s:
            try: s.close()
            except Exception: pass
def _tcp_open(ip, port):
    import socket
    try:
        c = socket.create_connection((ip, int(port)), timeout=3); c.close(); return True
    except Exception: return False
def _tcp_conn_count(g, port):
    """Terraria/Vanilla: Anzahl ESTABLISHED TCP-Verbindungen auf <port> dort, wo der Server
    laeuft (1 pro Spieler). Vanilla-Terraria hat kein Query-Protokoll -> so bekommt man trotzdem
    die echte Spielerzahl. -1 bei Fehler (ss fehlt/Ausfuehrung scheitert) -> Tick-Logik loest
    dann NIE faelschlich auto-off aus."""
    rc, out, _ = run(_in(g, "ss -Htn state established '( sport = :%s )'" % port))
    if rc != 0: return -1
    return sum(1 for line in out.splitlines() if line.strip())
def _rcon_players(g, ip, port):
    """Factorio: echte Spielerzahl via Source-RCON '/players online'. RCON-PW wird zur Laufzeit aus
    /etc/factorio/rcon.env am Spiel-Host gelesen (nie im Repo/games.json). -1 bei jedem Fehler (sicher)."""
    import socket, struct, re
    # %s einfach, nicht %%s: diese Zeile laeuft nicht mehr durch %-Formatierung (der Ort steckt
    # jetzt in _in), ein doppeltes Prozentzeichen kaeme woertlich bei printf an.
    rc, out, _ = run(_in(g, "sh -c 'set -a; . /etc/factorio/rcon.env 2>/dev/null; printf %s \"$RCON_PASSWORD\"'"))
    if rc != 0 or not out.strip(): return -1
    pw = out.strip()
    s = None
    try:
        def _send(sock, rid, typ, body):
            payload = struct.pack('<ii', rid, typ) + body.encode() + b'\x00\x00'
            sock.sendall(struct.pack('<i', len(payload)) + payload)
        def _recv(sock):
            raw = b''
            while len(raw) < 4:
                ch = sock.recv(4 - len(raw))
                if not ch: return None
                raw += ch
            ln = struct.unpack('<i', raw)[0]
            data = b''
            while len(data) < ln:
                ch = sock.recv(ln - len(data))
                if not ch: return None
                data += ch
            return struct.unpack('<ii', data[:8]) + (data[8:-2],)
        s = socket.create_connection((ip, int(port)), timeout=4); s.settimeout(4)
        _send(s, 1, 3, pw)                      # SERVERDATA_AUTH
        r = _recv(s)
        if not r or r[0] == -1: return -1       # Auth fehlgeschlagen (rid == -1)
        _send(s, 2, 2, "/players online")       # SERVERDATA_EXECCOMMAND
        r = _recv(s)
        if not r: return -1
        m = re.search(r"\((\d+)\)", r[2].decode(errors="replace"))   # "Online players (N):"
        return int(m.group(1)) if m else -1
    except Exception:
        return -1
    finally:
        if s:
            try: s.close()
            except Exception: pass
def game_host_ready(g):
    """Ist der Ort da, an dem das Spiel laeuft? Beim LXC heisst das 'Container laeuft'
    (spart ein pct-exec pro Tick), host-nativ ist es immer der laufende Wirt selbst."""
    return _lxc_running(g["ctid"]) if game_is_lxc(g) else True
def game_active(g):
    if not game_host_ready(g): return False
    k = g.get("kind", "lxc-systemd")
    if k in ("lxc-systemd", "systemd"):
        _, out, _ = run(_in(g, "systemctl is-active %s" % g["service"]))
        return out.strip() == "active"
    if k in ("lxc-docker", "docker"):
        _, out, _ = run(_in(g, "docker inspect -f '{{.State.Status}}' %s" % g["container"]))
        return out.strip() == "running"
    return False
def game_players(g):
    """echte Spielerzahl: a2s (Steam-Query) | tcp-conn (ESTABLISHED-Count, Terraria) |
    rcon (Factorio /players online). tcp -> nur Erreichbarkeit (1=up/0=down, keine Zahl). -1=Fehler."""
    p = g.get("probe", {})
    t = p.get("type")
    if t == "a2s": return _a2s_players(p["ip"], p["port"])
    if t == "tcp": return 1 if _tcp_open(p["ip"], p["port"]) else 0
    if t == "tcp-conn": return _tcp_conn_count(g, p["port"])
    if t == "rcon": return _rcon_players(g, p["ip"], p["port"])
    if t == "rcon-cli": return _rcon_cli_players(g)
    return -1
def _rcon_cli_players(g):
    """Minecraft: echte Spielerzahl via 'rcon-cli list' IM Container (itzg-Image bringt das
    Werkzeug mit, das Passwort steht dort schon in der Umgebung). Anders als die Source-RCON-
    Probe fuer Factorio braucht das keinen offenen Port und kein Passwort in der Registry.
    -1 bei jedem Fehler -> die Tick-Logik loest dann nie faelschlich ein Auto-Off aus."""
    behaelter = g.get("container") or g.get("probe", {}).get("container")
    if not behaelter: return -1
    rc, out, _ = run(_in(g, "docker exec %s rcon-cli list" % behaelter))
    if rc != 0: return -1
    m = re.search(r"There are (\d+)", out)
    return int(m.group(1)) if m else -1

def probe_counts_players(g):
    """True NUR wenn die Probe eine ECHTE Spielerzahl liefert (a2s/tcp-conn/rcon). Reines 'tcp' =
    nur up/down -> game_players()==1 heisst dann 'Port offen', NICHT '1 Spieler' (kein Auto-Off)."""
    return g.get("probe", {}).get("type") in ("a2s", "tcp-conn", "rcon", "rcon-cli")
def game_reachable(g):
    """Readiness (nur up/down, KEINE Spielerzahl): True, sobald der Server tatsaechlich joinbar
    ist: d.h. der Query/Port antwortet. Anders als game_players() (echte Spielerzahl); hier
    zaehlt nur 'antwortet der Server ueberhaupt schon'. Basis fuer den Discord-Ladebalken
    ('wann ist er da'): a2s/rcon -> Reply erhalten; tcp/tcp-conn -> Port lauscht."""
    p = g.get("probe", {}); t = p.get("type")
    if t == "a2s":  return _a2s_players(p["ip"], p["port"]) >= 0
    if t == "rcon": return _rcon_players(g, p["ip"], p["port"]) >= 0
    if t == "rcon-cli": return _rcon_cli_players(g) >= 0
    if t in ("tcp", "tcp-conn"): return _tcp_open(p["ip"], p["port"])
    return False
def game_ram_mb(g):
    """Grober Speicherbedarf des Spiels. Ohne eigenen ram_mb-Eintrag = min_free_mb, das war
    schon bisher die Hausnummer je Spiel."""
    return int(g.get("ram_mb") or g.get("min_free_mb", 4000))

def platzhalter_aktiv(g):
    """Laeuft der Platzhalter dieses Spiels? None, wenn es keinen gibt.

    Der Platzhalter ist bei schlafendem Server der EINZIGE Weckweg: Valheim/DayZ/Zomboid/
    Avorion verschwinden ohne ihn aus der Serverliste, Factorio hat ueberhaupt keinen
    anderen Zugang (weder A2S noch Listeneintrag). Faellt er aus, waehrend der Server
    schlaeft, ist das Spiel fuer Spieler tot -- und sieht von aussen genauso aus wie ein
    Spiel, das gerade niemand spielt. Genau diese Verwechslung soll die Metrik aufloesen.

    greeter_container deckt denselben Fall fuer Docker-Spiele ab: bei Minecraft haelt
    mc-velocity den Warteraum, ohne den niemand hereinkommt."""
    if g.get("greeter_service"):
        _, out, _ = run(_in(g, "systemctl is-active %s" % g["greeter_service"]))
        return out.strip() == "active"
    if g.get("greeter_container"):
        _, out, _ = run(_in(g, "docker inspect -f '{{.State.Status}}' %s" % g["greeter_container"]))
        return out.strip() == "running"
    return None

def dienst_lage(g):
    """ActiveState, SubState, Result und Speicher/CPU einer Spiel-Unit in EINEM Aufruf.

    ★ Der Grund fuer diese Funktion ist ein blinder Fleck, den game_active() nicht sehen
    kann: es fragt 'systemctl is-active' und bekommt bei einer gescheiterten Unit 'failed'
    -- also dasselbe 'nicht active' wie bei einem sauber schlafenden Server. Fuer den
    Arbiter sahen beide Faelle identisch aus, und weil der Platzhalter danach weiterlief,
    meldete spiel_weckbar unveraendert 1. Ein Spiel, das nach fuenf Fehlstarts in systemds
    StartLimit haengt (Restart=on-failure, StartLimitBurst=5/10s), war damit fuer Spieler
    tot und in der Ueberwachung gruen. Erst ActiveState+Result nebeneinander trennen
    'schlaeft' von 'kaputt'.

    Die Last-Werte kommen im selben Aufruf mit, weil sie dieselbe Frage an dieselbe Unit
    sind und ein 'pct exec' ~1,5 s kostet. MemoryCurrent/CPUUsageNSec sind der Ist-Wert
    gegen die BEHAUPTUNG ram_mb aus games.json -- die wurde einmal am 2026-08-22 gemessen
    und altert seitdem still mit jedem Mod, den jemand hinzufuegt.

    Rueckgabe: dict oder None (Docker-Spiele und LXC-Rollen ohne laufenden Host).
    'gescheitert' ist bewusst eng: NUR ActiveState=failed. Ein sauber gestopptes Spiel
    (inactive) ist kein Fehler, das ist der Normalzustand hier."""
    if g.get("kind") not in ("systemd", "lxc-systemd"): return None
    if not game_host_ready(g): return None
    rc, out, _ = run(_in(g, "systemctl show %s -p ActiveState -p SubState -p Result "
                             "-p MemoryCurrent -p CPUUsageNSec -p NRestarts" % g["service"]))
    if rc != 0: return None
    f = {}
    for zeile in out.splitlines():
        k, _, v = zeile.partition("=")
        f[k.strip()] = v.strip()
    if not f.get("ActiveState"): return None
    def zahl(k):
        v = f.get(k, "")
        # systemd meldet '[not set]' bzw. 'infinity', wenn die Erfassung fuer diese Unit
        # nicht laeuft. Als 0 durchgereicht waere das eine Messung, die keine ist.
        return int(v) if v.isdigit() else None
    return {
        "aktiv": f["ActiveState"] == "active",
        "gescheitert": f["ActiveState"] == "failed",
        "zustand": f["ActiveState"],
        "unterzustand": f.get("SubState", ""),
        "ergebnis": f.get("Result", ""),
        "speicher_bytes": zahl("MemoryCurrent"),
        "cpu_ns": zahl("CPUUsageNSec"),
        "neustarts": zahl("NRestarts"),
    }

def booting_reserve_mb(st, exclude=None):
    """Speicher, den gerade gestartete Spiele noch belegen WERDEN, aber noch nicht belegt haben.
    Ein Server braucht Minuten, bis er seine Welt im RAM hat; misst man in dieser Zeit nur den
    freien Speicher, kommen zwei schwere Starts kurz hintereinander beide durch und der
    Zweite killt den Ersten per OOM. Deshalb zaehlt der Bedarf fuer BOOT_RESERVE_S mit."""
    total = 0
    for n in reserved_games(st):
        if n == exclude: continue
        slot = game_slot(st, n) or {}
        since = slot.get("since")
        if not since or (time.time() - since) > BOOT_RESERVE_S: continue
        g = game_by_name(n)
        if g and not game_reachable(g):   # antwortet er schon, ist er da und im avail enthalten
            total += game_ram_mb(g)
    return total

def precheck_game(g, avail, st=None):
    if avail < 0: return True   # Sensorfehler -> nicht blockieren
    if st is not None: avail -= booting_reserve_mb(st, exclude=g.get("name"))
    return avail >= g.get("min_free_mb", 4000)
def game_start(g, world=None):
    if game_is_lxc(g) and not game_host_ready(g):
        act("Game '%s': LXC %d starten" % (g["name"], g["ctid"]), "pct start %d" % g["ctid"])
        if not DRY: time.sleep(4)
    if g.get("multi_world"):
        # Aktive Welt setzen + Welt-Dateien lazy anlegen (das Skript kommt aus dem
        # jeweiligen Game-Repo, validiert die ID selbst nochmal).
        # ★ world_script ist NICHT kosmetisch: auf Node .18 wohnte jedes Spiel in einem
        # eigenen LXC, weshalb ein fester Pfad je Spiel genau ein Skript traf. Auf gamehost
        # laufen alle host-nativ nebeneinander, dort haette ein zweites multi_world-Spiel
        # ueber denselben Pfad Terrarias Skript aufgerufen und dessen serverconfig.txt
        # ueberschrieben. Default bleibt der alte Pfad (Terraria/.18 unveraendert).
        w = world or world_info(g["name"])[0] or DEFAULT_WORLD
        if not WORLD_ID_RE.match(w or ""):
            # Backstop: w geht in eine Shell. Die Aufrufwege validieren bereits gegen die
            # Registry, aber ein ungeprueftes Feld darf hier nie durchrutschen.
            audit("[start:%s] ABGELEHNT: ungueltige Welt-ID '%s'" % (g["name"], w))
            return False
        ws = g.get("world_script") or "/usr/local/bin/ensure-world.sh"
        act("Game '%s': Welt '%s' vorbereiten (%s)" % (g["name"], w, os.path.basename(ws)),
            _in(g, "%s %s" % (ws, w)))
    gs = g.get("greeter_service")   # Port-teilender Kick-/Wake-Greeter (Terraria) -> VOR dem Server-Start freigeben
    if gs:
        act("Game '%s': Greeter '%s' stoppen (gibt Port frei fuer den echten Server)" % (g["name"], gs),
            _in(g, "systemctl stop %s" % gs))
    if g.get("kind") in ("lxc-docker", "docker"):
        # ACHTUNG: "container" wird doppelt benutzt -- als Compose-DIENSTname (compose up -d <x>)
        # und als CONTAINERname (docker inspect/exec <x>). Heissen die im Compose unterschiedlich,
        # startet der Arbiter zwar, sieht den Zustand aber nie und haelt das Spiel fuer aus.
        return act("Game '%s' starten (compose up -d %s)" % (g["name"], g["container"]),
                   _in(g, "bash -c 'cd %s && docker compose up -d %s'" % (g.get("compose_dir", "/opt/game"), g["container"])))
    return act("Game '%s' starten (systemctl start %s)" % (g["name"], g["service"]),
               _in(g, "systemctl start %s" % g["service"]))
def game_stop(g):
    if g.get("kind") in ("lxc-docker", "docker"):
        ok = act("Game '%s' graceful stoppen (compose stop %s)" % (g["name"], g["container"]),
                 _in(g, "bash -c 'cd %s && docker compose stop %s'" % (g.get("compose_dir", "/opt/game"), g["container"])), timeout=90)
    else:
        ok = act("Game '%s' graceful stoppen (systemctl stop %s, SIGINT)" % (g["name"], g["service"]),
                 _in(g, "systemctl stop %s" % g["service"]), timeout=90)
    gs = g.get("greeter_service")   # Port-teilender Greeter (Terraria) uebernimmt 7777 NACH dem Server-Stop
    if gs:
        # ★ Der Rueckgabewert zaehlt hier wirklich: schlaegt der Greeter-Start fehl, ist das
        # Spiel weder wach noch weckbar -- es verschwindet aus der Serverliste und niemand
        # kann es zurueckholen. Frueher lief das ungeprueft durch und der einzige Weckweg
        # konnte still verloren gehen. Ein zweiter Versuch deckt den haeufigsten Grund ab
        # (der eben beendete Server haelt den geteilten Port noch einen Moment).
        if not act("Game '%s': Greeter '%s' starten (zeigt 'wird gestartet' + weckt bei Beitritt)" % (g["name"], gs),
                   _in(g, "systemctl start %s" % gs)):
            if not DRY: time.sleep(3)
            if not act("Game '%s': Greeter '%s' zweiter Versuch (Port war womoeglich noch belegt)" % (g["name"], gs),
                       _in(g, "systemctl start %s" % gs)):
                audit("[warn] Game '%s': Platzhalter '%s' laeuft NICHT -- das Spiel ist jetzt "
                      "weder wach noch weckbar. Metrik spiel_weckbar meldet das." % (g["name"], gs))
    elif ok and game_is_lxc(g):
        # Kein Port-teilender Greeter: nach welt-sicherndem Dienst-Stop (ok) den LXC ganz
        # herunterfahren -> Node wird echt idle (sonst bleibt ein leerer Container laufend
        # zurueck, wie am 13.08. bei zomboid 208 beobachtet). Wake startet den Container
        # ohnehin per 'pct start' neu (game_start). Games MIT Greeter (Terraria) bleiben
        # oben, weil der Greeter den laufenden Container braucht.
        _shutdown_lxc(g["ctid"], g["name"])
    return ok

def _shutdown_lxc(ctid, label):
    """'pct shutdown' kehrt zurueck, sobald der Auftrag abgesetzt ist - nicht, wenn der Container
    wirklich unten ist. Am 20.08. meldete der Stopp von LXC 206 nach 3 s Erfolg, der Container lief
    danach noch stundenlang. Wie bei den Lab-VMs (_vm_running nach 'qm shutdown') wird deshalb
    nachgesehen und einmal nachgefasst. Kein Hard-Kill: haengt er weiter, sagt das Log es ehrlich,
    statt einen sauberen Node vorzutaeuschen."""
    if not act("Game '%s': leeren LXC %s herunterfahren (Node idle statt Rest-Container)" % (label, ctid),
               "pct shutdown %d --timeout 40" % ctid, timeout=70):
        return False
    if DRY: return True
    for versuch in (1, 2):
        time.sleep(5)
        rc, out, _ = run("pct status %d" % ctid)
        if rc != 0 or "running" not in out:
            return True
        if versuch == 1:
            audit("[game:%s] LXC %s laeuft nach dem Stopp noch -> einmal nachfassen" % (label, ctid))
            run("pct shutdown %d --timeout 40" % ctid, timeout=70)
    audit("[game:%s] LXC %s bleibt oben (Shutdown ohne Wirkung). Kostet wenig RAM, aber der Node ist "
          "nicht wirklich idle - manuell: pct stop %s" % (label, ctid, ctid))
    return False
def blocking_role(st, exclude_kind=None, exclude_name=None):
    """EIN Ort fuer 'wer hat gerade Vorrang?'. Gibt die schwere Rolle zurueck, die den Start einer
    NEUEN schweren Rolle blockiert, oder None (Node frei). Vorrang haben BELEGTE Rollen (ein Game
    oder MC mit >0 Spielern), RESERVIERTE Games und ein RESERVIERTES Win-Lab (bewusster Wunsch).
    Leere, ungeschuetzte Rollen blockieren NICHT -> sie weichen dem Neustart.
    exclude_kind/-name = die startende Rolle selbst (blockt sich nicht). Genutzt von cmd_start_game,
    cmd_start_lab UND cmd_wake_mc -> ein Kriterium, keine Duplikate.
    Rueckgabe: {"kind","name","players","reserviert"}|None."""
    for g in GAMES:
        if exclude_kind == "game" and g["name"] == exclude_name: continue
        if not game_active(g): continue
        # Der Schutz zaehlt VOR der Spielerzahl-Abfrage: er gilt gerade dann, wenn niemand
        # drauf ist, und spart im Trockenlauf die 5 s Wartezeit einer toten a2s-Probe.
        if game_geschuetzt(st, g["name"]):
            return {"kind": "game", "name": g["name"], "players": 0, "reserviert": True}
        gp = game_players(g)          # nur EINMAL fragen: a2s wartet im Fehlerfall 5 s
        if gp > 0:
            return {"kind": "game", "name": g["name"], "players": gp}
    if exclude_kind != "mc" and mc_lxc_running() and mc_ct_state() == "running" and mc_health() == "healthy":
        mp = mc_players()             # dito: 'pct exec docker exec rcon-cli' kostet ~1,5 s
        if mp > 0:
            return {"kind": "mc", "name": "minecraft", "players": mp}
    if exclude_kind != "lab" and st.get("reservation") == "lab" and lab_running():
        return {"kind": "lab", "name": "Windows-AD-Lab", "players": 0}
    return None
def role_label(r):
    """Menschliche Kurzform einer blocking_role fuer Discord-Meldung / Log."""
    return r["name"] + (" mit %d Spieler(n)" % r["players"] if r.get("players", 0) > 0 else "")

def absage_grund(r):
    """Warum diese Rolle den Start blockiert, im Klartext, wie er beim Menschen ankommt.

    Zwei verschiedene Gruende teilten sich frueher eine Meldung: 'dort wird gespielt' und
    'das ist reserviert' sind aber nicht dasselbe. Beim reservierten Lab hilft Warten nicht
    unbedingt, dort muss jemand den Wartungsmodus beenden. Wer nur 'ABGELEHNT' liest,
    sucht sonst nach einem Schalter, den es nicht mehr gibt."""
    if r.get("kind") == "lab":
        return ("das Windows-Lab ist im Wartungsmodus reserviert. Es bleibt geschuetzt, "
                "bis der Wartungsmodus beendet wird.")
    if r.get("reserviert"):
        return ("es ist reserviert und bleibt deshalb stehen, auch wenn gerade niemand darauf "
                "spielt. Die Reservierung aufheben kann ein Admin im Dashboard.")
    return ABSAGE_BELEGT + " " + ABSAGE_NACHSATZ

# ---------- rich Live-Snapshot fuer P4 (MQTT/Panel) ----------
def publish_mqtt(snap):
    """Publisht den Live-Snapshot retained nach MQTT, NUR wenn /opt/game-arbiter/mqtt.env
    existiert (MQTT_HOST/PORT/USER/PW/CAFILE/TOPIC). Ohne Datei = No-op (dormant bis Owner
    den Mosquitto-User anlegt + Creds hinterlegt). Fehler werden geschluckt (best-effort)."""
    envf = BASE + "/mqtt.env"
    if not os.path.exists(envf): return
    cfg = {}
    try:
        with open(envf) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1); cfg[k.strip()] = v.strip()
    except Exception: return
    host, user, pw = cfg.get("MQTT_HOST"), cfg.get("MQTT_USER"), cfg.get("MQTT_PW")
    if not (host and user and pw): return
    def _pub(topic, payload, retained):
        cmd = ["mosquitto_pub", "-h", host, "-p", cfg.get("MQTT_PORT", "8883"),
               "-u", user, "-P", pw, "-t", topic, "-m", payload]
        if retained: cmd.append("-r")
        if cfg.get("MQTT_CAFILE"): cmd += ["--cafile", cfg["MQTT_CAFILE"]]
        try: subprocess.run(cmd, capture_output=True, timeout=10)
        except Exception: pass
    # 1) Live-State retained -> Panel liest den letzten Stand beim Verbinden
    _pub(cfg.get("MQTT_TOPIC", "homelab/node18/orchestrator/state"), json.dumps(snap), True)
    # 2) Alert (nicht-retained) NUR bei echtem Problem -> brain-bus/ntfy, kein Spam pro Tick
    if snap.get("mode") == "FAILED":
        _pub("homelab/node18/orchestrator/alert",
             json.dumps({"node": "node18", "issue": "orchestrator FAILED (Crash-Latch)",
                         "ts": snap.get("ts"), "restart_count": snap.get("restart_count")}), False)

def _erreichbar(name, gemessene_spieler):
    """Antwortet dieses Spiel schon, oder bootet es noch? (True/False/None=unbekannt)

    ★ Die gemessene Spielerzahl allein reicht dafuer NICHT, und das haengt an der Probenart:
      a2s / rcon    eine Zahl >= 0 setzt eine echte Antwort voraus -> beweist Erreichbarkeit.
      tcp-conn      zaehlt bestehende Verbindungen. Ein Server, der noch gar nicht lauscht,
                    hat ebenso null davon wie ein laufender, auf dem niemand spielt. 0 beweist
                    hier also nichts: gemessen an Terraria sah ein frisch gestarteter Server
                    schon in der ersten Sekunde aus wie "laeuft, komm rein", waehrend er in
                    Wahrheit noch minutenlang seine Welt erzeugte.
    Nur in diesem mehrdeutigen Fall wird zusaetzlich gefragt, ob der Port ueberhaupt lauscht.
    Das ist hoechstens eine Probe je Tick und nur fuer reservierte tcp-conn-Spiele ohne Spieler.
    """
    g = game_by_name(name)
    if g is None or gemessene_spieler is None:
        return None
    if gemessene_spieler > 0:
        return True                       # jemand ist verbunden -> der Server steht
    if g.get("probe", {}).get("type") == "tcp-conn":
        return game_reachable(g)          # 0 Verbindungen sagt nichts -> Port direkt pruefen
    return gemessene_spieler >= 0         # a2s/rcon: 0 ist eine Antwort, -1 ist keine

def emit_state(st, lab, ct, health, players, avail, gate_up=None, game_players_known=None):
    """gate_up / game_players_known werden vom Tick durchgereicht (er hat sie gerade ermittelt).
    Nur wenn sie fehlen - etwa im FAILED-Zweig oder bei Direktaufruf - wird nachgefragt; das
    sparte pro Tick zwei teure 'pct exec'-Runden. game_players_known ist seit dem Mehr-Spiel-Umbau
    ein dict {spiel: spielerzahl} (-1 = gefragt, kein Kontakt)."""
    mc_reserved = st.get("reservation") == "minecraft"
    _res = reserved_games(st)
    # 'reserved' bleibt ein einzelner Name: wake-bridge, Dashboard und Bot lesen ihn seit
    # Monaten so. Die Wahrheit fuer mehrere Spiele steht daneben in reserved_all/detail,
    # additiv, damit ein alter Leser nichts falsch versteht statt zu brechen.
    _res_game = _res[0] if _res else None
    _spieler = dict(game_players_known or {})
    for n in _res:
        if n in _spieler: continue
        _g = game_by_name(n)
        if _g and probe_counts_players(_g) and game_active(_g):
            _spieler[n] = game_players(_g)
    _detail = {}
    for n in _res:
        slot = game_slot(st, n) or {}
        _p = _spieler.get(n)
        _detail[n] = {"since": slot.get("since"), "idle_since": slot.get("idle_since"),
                      "unused_since": slot.get("unused_since"), "was_used": bool(slot.get("was_used")),
                      "world": slot.get("world"), "players": (_p if (_p is not None and _p >= 0) else None),
                      # "erreichbar" trennt zwei Zustaende, die players=null bisher zusammenwarf:
                      # "gefragt, keine Antwort" (der Server bootet noch: DayZ braucht 2-3 min)
                      # und "antwortet, gerade leer". Fuer die Oberflaeche ist das der Unterschied
                      # zwischen "startet gerade" und "laeuft, komm rein"; wer nur players liest,
                      # zeigt beides als laufend an und schickt Spieler in einen Timeout.
                      # None = diese Rolle wurde gar nicht gefragt.
                      "erreichbar": _erreichbar(n, _spieler.get(n)),
                      # "geschuetzt" = im Dashboard 'Reserviert': kein Auto-Off, nicht verdraengbar.
                      # Ohne dieses Feld koennte die Oberflaeche einen Schutz anbieten, dessen
                      # Zustand sie nicht kennt, und den Knopf falsch herum beschriften.
                      "geschuetzt": game_geschuetzt(st, n)}
    _active_players = _detail.get(_res_game, {}).get("players") if _res_game else None
    snap = {
        "node": PROFILE["node"], "ts": now(), "mode": st["mode"],
        "mc": {"state": ct, "health": health, "players": (players if players >= 0 else None),
               "want": mc_reserved, "reserved": mc_reserved, "idle_since": st.get("mc_idle_since"),
               "unused_since": st.get("mc_unused_since")},
        "gate": {"up": (gate_running() if has_role("mc_gate") else False) if gate_up is None else gate_up},
        "lab": {"running": lab, "reserved": st.get("reservation") == "lab",
                "since": st.get("lab_since"), "idle_since": st.get("lab_idle_since"),
                "idle_timeout_s": CFG["lab_idle_timeout_s"]},
        "games": {"reserved": _res_game,
                  "reserved_all": _res,
                  "detail": _detail,
                  "registry": game_names(),
                  "active_players": _active_players,
                  "idle_since": (_detail.get(_res_game) or {}).get("idle_since") if _res_game else None,
                  "unused_since": (_detail.get(_res_game) or {}).get("unused_since") if _res_game else None,
                  # Multi-Welten: je Game aktive Welt + Registry (nur Games mit multi_world)
                  "worlds": {n: {"active": world_info(n)[0], "list": world_info(n)[1]}
                             for n in game_names() if game_multi_world(n)},
                  # Was jedes Spiel braucht, um starten zu duerfen. Steht hier, damit eine
                  # Oberflaeche VOR dem Klick sagen kann "dafuer reicht der Speicher gerade
                  # nicht" statt den Nutzer in eine Absage laufen zu lassen, und zwar mit
                  # DEN Zahlen, gegen die precheck_game() wirklich prueft. Wer sie stattdessen
                  # im Dashboard nachpflegt, fuehrt eine zweite Wahrheit, die beim naechsten
                  # ram_mb-Nachmessen still falsch wird.
                  "bedarf": {g["name"]: {"ram_mb": game_ram_mb(g),
                                         "min_free_mb": g.get("min_free_mb", 4000)}
                             for g in GAMES},
                  # Wartung: hier, damit Dashboard und Bot einen Weckknopf ausgrauen koennen,
                  # statt den Nutzer in eine Absage laufen zu lassen. Leeres dict = keine.
                  "wartung": {n: {"seit": (game_in_wartung(st, n) or {}).get("seit"),
                                  "grund": (game_in_wartung(st, n) or {}).get("grund")}
                              for n in wartung_games(st)}},
        # reserviert_startend_mb: Speicher, den gerade gestartete Spiele noch belegen WERDEN.
        # precheck_game zieht ihn ab, also muss eine Startbarkeits-Vorschau das auch tun --
        # sonst zeigt sie "startbar", wo der Arbiter Sekunden spaeter absagt.
        "ram": {"avail_mb": avail, "min_free_mc_mb": CFG["min_free_mb_for_mc"],
                "reserviert_startend_mb": booting_reserve_mb(st)},
        "restart_count": st.get("restart_count", 0),
    }
    try:
        tmp = STATUS_FILE + ".tmp"
        with open(tmp, "w") as f: json.dump(snap, f, indent=2)
        os.replace(tmp, STATUS_FILE)
    except Exception as e:
        audit("[warn] status.json schreiben fehlgeschlagen: %s" % e)
    metriken_schreiben(snap, st)
    publish_mqtt(snap)   # retained -> homelab/node18/orchestrator/state (dormant ohne mqtt.env)

def metriken_schreiben(snap, st):
    """Spiel-Zustand als Prometheus-Metriken fuer den node-exporter.

    Beantwortet die eine Frage, die von aussen bisher niemand beantworten konnte:
    'kommt gerade jemand in dieses Spiel hinein?' Ein schlafendes Spiel und ein kaputtes
    sehen im Netz identisch aus -- beide antworten nicht. Erst Server- UND Platzhalter-
    Zustand nebeneinander trennen 'schlaeft, weckbar' von 'weder da noch weckbar'.

    Der Selbsttest kommt gratis mit: der node-exporter liefert zu jeder Datei ein
    node_textfile_mtime_seconds. Bleibt der Arbiter stehen, altert die Datei und die
    Alarmregel sieht das -- ohne dass der Waechter seinen eigenen Ausfall melden muesste
    (was er im Ausfall gerade nicht mehr kann).

    Geschrieben wird nur, wenn das Verzeichnis existiert -> auf Node .18 ohne node-exporter
    passiert nichts. Fehler sind hier nie fatal: die Beobachtung darf den Betrieb der
    Spiele nicht gefaehrden."""
    ordner = os.path.dirname(METRICS_FILE)
    if not os.path.isdir(ordner): return
    try:
        z = []
        def m(name, hilfe, werte, typ="gauge"):
            if not werte: return
            z.append("# HELP %s %s" % (name, hilfe))
            z.append("# TYPE %s %s" % (name, typ))
            z.extend("%s{spiel=\"%s\"} %s" % (name, n, v) for n, v in werte)
        detail = (snap.get("games") or {}).get("detail") or {}
        bedarf = (snap.get("games") or {}).get("bedarf") or {}
        avail = (snap.get("ram") or {}).get("avail_mb", -1)
        reserviert = (snap.get("ram") or {}).get("reserviert_startend_mb", 0)
        server, platz, weckbar, spieler, schutz, startbar, braucht = [], [], [], [], [], [], []
        kaputt, speicher, cpu, neustarts, fehlstarts, wartung = [], [], [], [], [], []
        for g in GAMES:
            n = g["name"]
            # 'aktiv' bleibt game_active -- eine Quelle fuer diese Frage, im ganzen Modul.
            # dienst_lage liefert daneben, was sie nicht sehen kann: 'failed' gegen
            # 'schlaeft', dazu Speicher und CPU der laufenden Unit.
            akt = game_active(g)
            lage = dienst_lage(g)
            ph = platzhalter_aktiv(g)
            server.append((n, int(akt)))
            if ph is not None:
                platz.append((n, int(ph)))
                weckbar.append((n, int(akt or ph)))
            if lage:
                # Immer schreiben, auch die 0. Eine Metrik, die nur im Fehlerfall erscheint,
                # laesst sich nicht von einer abgeschalteten Messung unterscheiden -- der
                # Alarm haette dann keine Reihe, gegen die er pruefen koennte.
                kaputt.append((n, int(lage["gescheitert"])))
                if lage["neustarts"] is not None: neustarts.append((n, lage["neustarts"]))
                if akt and lage["speicher_bytes"] is not None:
                    speicher.append((n, lage["speicher_bytes"]))
                if akt and lage["cpu_ns"] is not None:
                    cpu.append((n, "%.3f" % (lage["cpu_ns"] / 1e9)))
            fs = (game_slot(st, n) or {}).get("startfehler") or 0
            fehlstarts.append((n, int(fs)))
            p = (detail.get(n) or {}).get("players")
            if p is not None: spieler.append((n, int(p)))
            schutz.append((n, int(game_geschuetzt(st, n))))
            braucht.append((n, int(g.get("min_free_mb", 4000))))
            wartung.append((n, int(bool(game_in_wartung(st, n)))))
            # Dieselbe Rechnung wie precheck_game, damit die Metrik nicht 'startbar' zeigt,
            # wo der Arbiter Sekunden spaeter absagt.
            startbar.append((n, int(avail < 0 or (avail - reserviert) >= g.get("min_free_mb", 4000))))
        m("spiel_server_aktiv", "Der Spielserver selbst laeuft (1) oder schlaeft (0).", server)
        m("spiel_platzhalter_aktiv",
          "Der Platzhalter/Weckposten laeuft (1). Nur fuer Spiele, die einen haben.", platz)
        m("spiel_weckbar",
          "Spieler kommen an dieses Spiel heran: Server laeuft ODER Platzhalter haelt es "
          "sichtbar und weckt (1). 0 = fuer Spieler tot, auch wenn der Wirt gesund ist.", weckbar)
        m("spiel_dienst_gescheitert",
          "Die Unit steht in 'failed' (1). Unterscheidet den kaputten Server vom schlafenden: "
          "beide sind 'nicht aktiv', aber nur einer kommt beim Wecken wieder hoch.", kaputt)
        m("spiel_startfehler_in_folge",
          "Wie oft der Arbiter dieses Spiel hintereinander vergeblich gestartet hat. 0 nach "
          "dem ersten Erfolg. Waechst der Wert, laeuft ein Weckversuch ins Leere.", fehlstarts)
        m("spiel_unit_neustarts_gesamt",
          "NRestarts der Unit: wie oft systemd sie seit dem letzten manuellen Start wieder "
          "hochgezogen hat. Steigt bei einem Server, der wiederholt abstuerzt.", neustarts, "counter")
        m("spiel_speicher_bytes",
          "Gemessener Speicher der laufenden Unit (MemoryCurrent). Gegenstueck zur Behauptung "
          "spiel_bedarf_mb aus games.json, die nur bei der Einrichtung gemessen wurde.", speicher)
        m("spiel_cpu_sekunden_gesamt",
          "Verbrauchte CPU-Zeit der laufenden Unit (CPUUsageNSec).", cpu, "counter")
        m("spiel_spieler", "Gemessene Spielerzahl (nur bei geweckten Spielen vorhanden).", spieler)
        m("spiel_geschuetzt", "Reserviert: kein Auto-Off, keine Verdraengung (1).", schutz)
        m("spiel_startbar", "Der freie Speicher reicht fuer einen Start dieses Spiels (1).", startbar)
        m("spiel_wartung",
          "Wartung gesetzt: jeder Weckversuch wird abgelehnt (1). Der Platzhalter laeuft "
          "weiter, spiel_weckbar meldet also 1, obwohl gerade niemand hineinkommt. Ohne "
          "diese Reihe waere eine vergessene Wartung von aussen nicht von einem gesunden "
          "schlafenden Spiel zu unterscheiden.", wartung)
        # Wie lange die aelteste Wartung schon laeuft. Eine Wartung ist ein Zustand, den ein
        # Mensch setzt und vergessen kann, und ihr Preis ist ein Spiel, das niemand mehr
        # starten kann. Die Alarmregel haengt an dieser Zahl, nicht an spiel_wartung.
        _wseit = [(n, int(time.time() - ((game_in_wartung(st, n) or {}).get("seit") or time.time())))
                  for n in wartung_games(st)]
        m("spiel_wartung_sekunden", "Dauer der laufenden Wartung dieses Spiels.", _wseit)
        m("spiel_bedarf_mb", "Freier Speicher, den ein Start dieses Spiels voraussetzt.", braucht)
        z.append("# HELP arbiter_speicher_frei_mb Freier Speicher, gegen den der Arbiter Starts abwaegt.")
        z.append("# TYPE arbiter_speicher_frei_mb gauge")
        z.append("arbiter_speicher_frei_mb %d" % avail)
        z.append("# HELP arbiter_tick_zeitpunkt_sekunden Unix-Zeit des letzten Arbiter-Durchlaufs.")
        z.append("# TYPE arbiter_tick_zeitpunkt_sekunden gauge")
        z.append("arbiter_tick_zeitpunkt_sekunden %d" % int(time.time()))
        tmp = METRICS_FILE + ".tmp"
        with open(tmp, "w") as f: f.write("\n".join(z) + "\n")
        os.chmod(tmp, 0o644)     # der node-exporter laeuft als eigener Nutzer, nicht als root
        os.replace(tmp, METRICS_FILE)
    except Exception as e:
        audit("[warn] Spiel-Metriken schreiben fehlgeschlagen: %s" % e)

# ---------- Controller-Tick ----------
def tick(st, confirm_evict, confirm_hard):
    if st["mode"] == "FAILED":
        audit("[FAILED] eingefroren (restart_count=%d). Manueller --reset noetig." % st["restart_count"])
        emit_state(st, lab_running() if has_role("lab") else [],
                   mc_ct_state() if has_role("minecraft") else "absent",
                   "n/a", -1, free_mb()); return st

    # Rollen, die es an diesem Ort gar nicht gibt (Spiele-VPS: kein Minecraft, kein Win-Lab),
    # werden nicht gefragt. Ohne diese Weiche liefe jeder Tick in ein fehlendes pct/qm und
    # meldete 'absent'/'[]': richtig geraten, aber teuer erkauft und irrefuehrend im Log.
    mc_on, lab_on = has_role("minecraft"), has_role("lab")
    gate_up = ensure_gate() if has_role("mc_gate") else None
    lab = lab_running() if lab_on else []
    lab_active = bool(lab)
    mc_up = mc_lxc_running() if mc_on else False
    ct = mc_ct_state() if mc_up else "absent"
    res = st.get("reservation", "none")
    mc_wanted = mc_on and res == "minecraft"
    health = "n/a"; ec = None; players = -1
    if ct == "running":
        health = mc_health(); players = mc_players()
    elif ct == "exited" and mc_wanted:
        # Exit-Code wird nur in der Crash-Maschine (MC reserviert) ausgewertet. Bei schlafendem,
        # unreserviertem MC war das jeden Tick ein 'pct exec' fuer eine Zahl, die niemand liest.
        ec = mc_exit_code()
    avail = free_mb()

    # Lab-Sitzungsuhr pflegen
    if lab_active and not st.get("lab_since"):
        st["lab_since"] = time.time(); st["lab_idle_since"] = None
        audit("[lab] Win-Lab %s erkannt -> Sitzung laeuft (Idle-Auto-Off nach %ds ohne Sitzung, wenn unreserviert)" % (lab, CFG["lab_idle_timeout_s"]))
    if not lab_active and st.get("lab_since"):
        st["lab_since"] = None; st["lab_idle_since"] = None; audit("[lab] Win-Lab beendet -> Sitzungsuhr aus")

    # Lab Idle-Auto-Off (nur graceful, nie hart; reserviertes Lab geschuetzt): faehrt runter, wenn
    # KEINE aktive Sitzung mehr fuer lab_idle_timeout_s besteht (nicht mehr fix seit Start).
    if lab_active and res != "lab":
        if lab_session_active():
            if st.get("lab_idle_since"): audit("[lab] wieder aktive Sitzung -> Idle-Timer zurueckgesetzt")
            st["lab_idle_since"] = None
        else:
            if not st.get("lab_idle_since"):
                st["lab_idle_since"] = time.time()
                audit("[lab] keine aktive Sitzung -> Idle-Timer laeuft (Auto-Off nach %ds)" % CFG["lab_idle_timeout_s"])
            idle = int(time.time() - st["lab_idle_since"])
            if idle >= CFG["lab_idle_timeout_s"]:
                audit("[auto-off] Lab %ds idle >= %ds -> graceful herunterfahren (kein Hard-Kill)" % (idle, CFG["lab_idle_timeout_s"]))
                if stop_lab_graceful(lab):
                    audit("[auto-off] Lab aus -> RAM frei, Node idle (on-demand, nichts weckt automatisch)"); lab = []; lab_active = False
                    st["lab_since"] = None; st["lab_idle_since"] = None
                else:
                    audit("[auto-off] graceful gescheitert (kein Guest-Agent / leere Shell) -> Lab bleibt; Retry in %ds, manuell: --evict-lab" % CFG["lab_idle_timeout_s"])
                    st["lab_idle_since"] = time.time()   # Backoff: nicht jeden Tick erneut versuchen

    # Effektiv gewuenschter MC-Zustand: MC ist eine on-demand-Rolle wie jedes Game und laeuft
    # NUR, wenn ausdruecklich reserviert (reservation=='minecraft'). Sonst weicht es -> Node idle,
    # wenn niemand spielt. Das Wecken/Verdraengen macht cmd_wake_mc; hier nur der Soll-Zustand.
    # (Frueher: 'kein Lab -> immer-online' -> entfernt 2026-08-07, alle Games gleichrangig.)
    if mc_wanted:
        eff, reason = True, "minecraft reserviert -> MC on-demand aktiv"
    else:
        eff, reason = False, "minecraft nicht reserviert (res=%s) -> MC weicht (on-demand, Node ggf. idle)" % res

    prev = st["mode"]; new = prev
    snap = ("eff=%s lab=%s ct=%s ec=%s health=%s players=%s avail=%dMB rc=%d res=%s | %s"
            % (eff, lab, ct, ec, health, players, avail, st["restart_count"], res, reason))

    if not eff:
        st["idle_since"] = None
        if ct == "running":
            new = "MINECRAFT_STOPPING"; mc_stop()
        else:
            st["restart_count"] = 0
            new = "LAB_ACTIVE" if lab_active else "IDLE"
    else:  # eff == True -> MC soll laufen
        if ct == "running" and health == "healthy":
            st["restart_count"] = 0; new = "MINECRAFT_ACTIVE"
            # MC Idle-Auto-Off (symmetrisch zu Games): echte Spielerzahl via rcon list. Erst nach
            # erster Nutzung (mc_was_used), dann idle_timeout_s leer -> stoppen + Reservation zurueck.
            ito = CFG["idle_timeout_s"]; uto = CFG.get("unused_timeout_s", DEFAULT_UNUSED_TIMEOUT_S)
            if players > 0:
                st["mc_idle_since"] = None; st["mc_unused_since"] = None; st["mc_was_used"] = True
            elif players == 0 and st.get("mc_was_used") and ito > 0:
                if not st.get("mc_idle_since"): st["mc_idle_since"] = time.time()
                idle = int(time.time() - st["mc_idle_since"])
                if idle >= ito:
                    audit("[mc] leer >= %ds nach Nutzung -> auto-off (RAM zurueck, Node idle)" % ito)
                    mc_stop(); st["reservation"] = "none"; st["mc_idle_since"] = None
                    st["mc_was_used"] = False; new = "MINECRAFT_STOPPING"
                else:
                    audit_status(st, "mc", "idle", "[mc] aktiv, leer seit %ds nach Nutzung (auto-off bei %ds)" % (idle, ito))
            elif players == 0 and ito > 0 and uto > 0:
                # Geweckt, healthy (= joinbar), aber noch nie betreten: Grace-Timer. Ohne ihn blieb
                # ein versehentlich geweckter Server bis zum manuellen Stopp stehen.
                st["mc_idle_since"] = None
                if not st.get("mc_unused_since"): st["mc_unused_since"] = time.time()
                un = int(time.time() - st["mc_unused_since"])
                if un >= uto:
                    audit("[mc] seit %ds joinbar, aber nie betreten -> auto-off (geweckt und vergessen)" % un)
                    mc_stop(); st["reservation"] = "none"; st["mc_unused_since"] = None
                    st["mc_was_used"] = False; new = "MINECRAFT_STOPPING"
                else:
                    audit_status(st, "mc", "unused", "[mc] wartet auf Spieler, seit %ds joinbar (auto-off bei %ds)" % (un, uto))
            elif players == 0:
                st["mc_idle_since"] = None   # Auto-Off bewusst aus (ito<=0) -> laeuft weiter
        elif ct == "running" and health in ("starting", "n/a"):
            new = "MINECRAFT_STARTING"
        elif ct == "running" and health == "unhealthy":
            st["restart_count"] += 1
            if st["restart_count"] > CFG["max_restarts"]:
                new = "FAILED"; audit("[heal] unhealthy > %d Restarts -> FAILED" % CFG["max_restarts"])
            else:
                new = "MINECRAFT_STARTING"; audit("[heal] unhealthy -> Restart %d/%d" % (st["restart_count"], CFG["max_restarts"]))
                mc_stop(); mc_start()
        else:  # exited/absent obwohl gewollt -> Absturz oder (Wieder-)Start
            crash = (ec is not None and ec != 0)
            if prev in ("MINECRAFT_ACTIVE", "MINECRAFT_STARTING") or crash:
                st["restart_count"] += 1
                if st["restart_count"] > CFG["max_restarts"]:
                    new = "FAILED"; audit("[heal] Absturz (ec=%s) > %d Restarts -> FAILED" % (ec, CFG["max_restarts"]))
                elif precheck_mc(avail):
                    new = "MINECRAFT_STARTING"; audit("[heal] Absturz (ec=%s) -> Restart %d/%d" % (ec, st["restart_count"], CFG["max_restarts"])); mc_start()
                else:
                    new = "MINECRAFT_STARTING"; audit("[heal] Restart aufgeschoben (RAM knapp: %dMB)" % avail)
            else:  # Erststart / Wecken nach Lab
                if precheck_mc(avail):
                    new = "MINECRAFT_STARTING"; mc_start()
                else:
                    new = "IDLE"; audit("[precheck] avail %dMB < %dMB -> MC-Start aufgeschoben" % (avail, CFG["min_free_mb_for_mc"]))

    # ---- On-Demand-Games: jedes reservierte Spiel mit EIGENEN Uhren ----
    # Bis 2026-08-22 durfte genau eines laufen (reservation als String), alles andere raeumte
    # dieser Block ab. Auf dem Spiele-VPS laufen mehrere nebeneinander; die Bremse ist
    # min_free_mb je Spiel, nicht die Exklusivitaet. Die Zweiglogik darin ist unveraendert,
    # sie liest ihre Uhren nur aus dem Spiel-Eintrag statt aus dem gemeinsamen State.
    res_game_players = {}
    for g in GAMES:
        gn = g["name"]
        g_aktiv = game_active(g)
        # ★ Die Lage wird NUR gefragt, wenn das Spiel nicht laeuft. Zwei Gruende: bei einem
        # laufenden Server gibt es nichts zu unterscheiden (die Metrik holt Last und Zustand
        # ohnehin separat), und 'aktiv oder nicht' bleibt bewusst bei game_active -- die eine
        # erprobte Stelle, gegen die auch die Testsuite prueft. dienst_lage ergaenzt sie um
        # das, was sie nicht sehen kann ('failed' gegen 'schlaeft'), ersetzt sie aber nicht.
        lage = None if g_aktiv else dienst_lage(g)
        slot = game_slot(st, gn)
        if slot is None:
            if lage and lage["gescheitert"]:
                # Unreserviert UND gescheitert: niemand will das Spiel gerade, aber die Unit
                # steht in 'failed'. Fuer den Arbiter war das bisher ununterscheidbar von
                # 'schlaeft sauber' -- und weil der Platzhalter weiterlaeuft, meldete
                # spiel_weckbar 1. Der naechste Spieler weckt dann ins Leere. Zuruecksetzen
                # ist ungefaehrlich (es laeuft nichts) und macht den Weckweg wieder frei.
                audit("[game:%s] Unit steht in 'failed' (Result=%s) ohne Reservierung -> "
                      "Fehlerzustand zuruecksetzen, damit der naechste Weckversuch nicht ins "
                      "Leere laeuft" % (gn, lage["ergebnis"] or "?"))
                act("Game '%s': gescheiterte Unit zuruecksetzen (systemctl reset-failed)" % gn,
                    _in(g, "systemctl reset-failed %s" % g["service"]))
                continue
            if not g_aktiv:
                continue
            gp = game_players(g) if probe_counts_players(g) else -1
            if gp > 0:
                # SICHERHEITSGURT: ein besetztes Spiel wird nie abgeraeumt. Frueher stoppte
                # dieser Zweig bedingungslos alles Unreservierte, auf einem Host, auf dem
                # Spiele auch von Hand oder beim Systemstart hochkommen, wirft das Spieler
                # mitten aus der Partie. Stattdessen uebernimmt der Arbiter es.
                audit("[game:%s] laeuft mit %d Spieler(n) ohne Reservierung -> uebernommen statt gestoppt" % (gn, gp))
                slot = reserve_game(st, gn, was_used=True)
            else:
                audit("[game:%s] laeuft leer und ohne Reservierung -> graceful stoppen (aufraeumen, RAM frei)" % gn)
                game_stop(g)
                continue
        if not g_aktiv:
            # ★ Ein Startversuch, der nicht haelt, war bisher unsichtbar: 'systemctl start'
            # kehrt bei Type=simple sofort mit rc=0 zurueck, lange bevor der Server steht --
            # der Rueckgabewert bestaetigt also die Absicht, nicht die Wirkung. Scheiterte
            # der Dienst danach, probierte dieser Zweig es im Minutentakt endlos weiter,
            # ohne Zaehler, ohne Meldung und ohne dass irgendeine Metrik davon wusste.
            # Deshalb wird die Quittung im FOLGENDEN Tick eingeholt: steht die Unit
            # START_QUITTUNG_S nach dem Versuch immer noch nicht, hat er nicht gehalten.
            if lage and lage["zustand"] == "activating":
                # Startet gerade (ExecStartPre laedt z.B. ein Update) -> kein Fehlversuch.
                audit_status(st, "game", gn + ":activating",
                             "[game:%s] Unit startet gerade (%s) -> abwarten" % (gn, lage["unterzustand"] or "activating"))
                continue
            letzter = slot.get("start_versuch")
            if letzter and (time.time() - letzter) >= START_QUITTUNG_S:
                slot["startfehler"] = int(slot.get("startfehler") or 0) + 1
                slot["start_versuch"] = None
                audit("[game:%s] Startversuch vor %ds hat nicht gehalten (Unit: %s/%s) -> "
                      "Fehlversuch %d in Folge"
                      % (gn, int(time.time() - letzter), (lage or {}).get("zustand", "?"),
                         (lage or {}).get("ergebnis", "?"), slot["startfehler"]))
            fehler = int(slot.get("startfehler") or 0)
            naechster = slot.get("naechster_versuch") or 0
            if fehler and time.time() < naechster:
                # Backoff: Es wird NIE ganz aufgegeben -- sonst kaeme das Spiel nach einer
                # behobenen Ursache nie von selbst zurueck, und genau das soll ein Mensch
                # nicht von Hand nachholen muessen. Nur der Abstand waechst, damit ein
                # kaputtes Spiel nicht im Minutentakt Last erzeugt und das Log flutet.
                audit_status(st, "game", "%s:backoff=%d" % (gn, fehler),
                             "[game:%s] %d Fehlstarts in Folge -> naechster Versuch in %ds "
                             "(Metrik spiel_startfehler_in_folge meldet das)"
                             % (gn, fehler, int(naechster - time.time())))
                continue
            if lage and lage["gescheitert"]:
                # Reisst die Unit systemds StartLimit (Result=start-limit-hit), lehnt ein
                # weiteres 'start' mit 'repeated too quickly' ab, bis jemand reset-failed
                # ruft. In den anderen Fehlerfaellen laeuft der Start auch ohne, dann raeumt
                # reset-failed nur den Zustand auf -- schaden kann es in keinem Fall, weil
                # es lediglich die Fehlermarkierung einer nicht laufenden Unit loescht.
                act("Game '%s': gescheiterte Unit zuruecksetzen (Result=%s)" % (gn, lage["ergebnis"] or "?"),
                    _in(g, "systemctl reset-failed %s" % g["service"]))
            wtg = game_in_wartung(st, gn)
            if wtg:
                # Eine Reservierung bleibt bestehen, waehrend jemand am Spiel arbeitet: sie
                # sagt "diese Rolle gehoert jemandem", die Wartung sagt "sie darf gerade
                # nicht hochkommen". Ohne diese Zeile holte der Tick sich jede Minute
                # zurueck, was das Update-Werkzeug gerade heruntergefahren hat.
                audit_status(st, "game", "%s:wartung" % gn,
                             "[game:%s] reserviert, aber in Wartung -> kein Start. %s"
                             % (gn, wartung_text(wtg)))
                continue
            if precheck_game(g, avail, st):
                audit("[game:%s] reserviert aber aus -> Start%s"
                      % (gn, (" (Versuch %d nach Fehlstarts)" % (fehler + 1)) if fehler else ""))
                game_start(g, world=slot.get("world"))
                # Start-Fenster auch hier setzen, nicht nur bei --wake: nach einem Neustart des
                # Wirts sind alle Reservierungen noch da und der Tick startet sie der Reihe nach.
                # Ohne diese Zeile saehe jeder folgende Start denselben, noch unverbrauchten
                # freien Speicher -- und der Wirt bekaeme in einem Durchgang mehr Spiele, als
                # er tragen kann.
                slot["since"] = time.time()
                slot["start_versuch"] = time.time()
                slot["naechster_versuch"] = time.time() + START_QUITTUNG_S + backoff_s(fehler + 1)
                slot["idle_since"] = None; slot["unused_since"] = None
            else:
                audit("[game:%s] Start aufgeschoben (RAM knapp: %dMB < %dMB)" % (gn, avail, g.get("min_free_mb", 4000)))
            continue
        if slot.get("startfehler") or slot.get("start_versuch"):
            # Die Unit steht -> der letzte Versuch hat gehalten. Zaehler und Backoff fallen
            # zusammen zurueck, sonst bremste eine laengst behobene Ursache den naechsten
            # echten Fehlstart noch mit halbstuendigem Abstand aus.
            if slot.get("startfehler"):
                audit("[game:%s] laeuft wieder nach %d Fehlstart(en) -> Zaehler zurueck"
                      % (gn, int(slot["startfehler"])))
            slot["startfehler"] = 0; slot["start_versuch"] = None; slot["naechster_versuch"] = 0
        gp = game_players(g); ito = g.get("idle_timeout_s", 0)
        uto = g.get("unused_timeout_s", DEFAULT_UNUSED_TIMEOUT_S)
        if game_geschuetzt(st, gn):
            # Reserviert: kein Auto-Off, egal wie lange leer. Die Uhren werden dabei
            # ZURUECKGESETZT statt nur uebersprungen, sonst stuende beim Freigeben eine
            # abgelaufene idle_since im Slot und das Spiel ginge im selben Tick aus, in dem
            # der Schutz faellt. Wer freigibt, erwartet die volle Frist, nicht das Fallbeil.
            slot["idle_since"] = None; slot["unused_since"] = None
            if gp > 0: slot["was_used"] = True
            res_game_players[gn] = gp
            audit_status(st, "game", "%s:reserviert=%d" % (gn, gp),
                         "[game:%s] reserviert -> bleibt an (Auto-Off ausgesetzt, %s)"
                         % (gn, ("%d Spieler online" % gp) if gp > 0 else "gerade leer"))
            continue
        counts = probe_counts_players(g)   # a2s/tcp-conn/rcon -> echte Zahl; tcp -> nur up/down
        # auch -1 durchreichen: 'gefragt, kein Kontakt' ist eine Antwort. Sonst haelt
        # emit_state es fuer 'nicht gefragt' und probt in der Bootphase ein zweites Mal
        # gegen einen Server, der noch schweigt - das kostet jedes Mal den 5-s-Timeout.
        if counts: res_game_players[gn] = gp
        if not counts:
            # tcp-Probe misst nur Erreichbarkeit, KEINE Spielerzahl -> nie auto-off per Leere
            slot["idle_since"] = None; slot["unused_since"] = None
            if gp > 0: audit_status(st, "game", gn + ":up", "[game:%s] aktiv (Port offen; Spielerzahl via tcp-Probe nicht messbar)" % gn)
            else: audit_status(st, "game", gn + ":boot", "[game:%s] Dienst aktiv, Probe-Port noch nicht offen (startet?)" % gn)
        elif gp > 0:
            slot["idle_since"] = None; slot["unused_since"] = None; slot["was_used"] = True
            audit_status(st, "game", "%s:players=%d" % (gn, gp), "[game:%s] aktiv, %d Spieler online" % (gn, gp))
        elif gp == 0 and slot.get("was_used") and ito > 0:
            if not slot.get("idle_since"): slot["idle_since"] = time.time()
            idle = int(time.time() - slot["idle_since"])
            if idle >= ito:
                audit("[game:%s] leer >= %ds nach Nutzung -> auto-off (RAM zurueck)" % (gn, ito))
                game_stop(g); release_game(st, gn)
            else:
                audit_status(st, "game", gn + ":idle", "[game:%s] aktiv, leer seit %ds nach Nutzung (auto-off bei %ds)" % (gn, idle, ito))
        elif gp == 0 and ito > 0 and uto > 0:
            # Geweckt, joinbar (die Probe antwortet), aber noch nie betreten -> Grace-Timer.
            # Dieser Zweig lief frueher unbegrenzt weiter und war die Quelle der 63,6 h Leerlauf.
            slot["idle_since"] = None
            if not slot.get("unused_since"): slot["unused_since"] = time.time()
            un = int(time.time() - slot["unused_since"])
            if un >= uto:
                audit("[game:%s] seit %ds joinbar, aber nie betreten -> auto-off (geweckt und vergessen)" % (gn, un))
                game_stop(g); release_game(st, gn)
            else:
                audit_status(st, "game", gn + ":unused", "[game:%s] wartet auf Spieler, seit %ds joinbar (auto-off bei %ds)" % (gn, un, uto))
        elif gp == 0:
            slot["idle_since"] = None; slot["unused_since"] = None
            audit_status(st, "game", gn + ":always-on", "[game:%s] aktiv, leer -> bleibt an (Auto-Off aus, immer-online)" % gn)
        else:
            slot["unused_since"] = None   # kein Kontakt -> Server bootet oder haengt, nicht 'leer'
            audit_status(st, "game", gn + ":noprobe", "[game:%s] aktiv, Spielerzahl unbekannt (Probe kein Kontakt)" % gn)

    if new != prev:
        audit("TRANSITION %s -> %s | %s" % (prev, new, snap))
        st.setdefault("log_last", {})["mode"] = {"key": None, "ts": time.time()}   # danach wieder voll loggen
    else:
        # Schluessel ohne die staendig schwankenden Zahlen (avail schwankt um ein paar MB) ->
        # eine unveraenderte Lage erzeugt keine 1440 identischen Zeilen pro Tag mehr.
        key = "%s|%s|%s|%s|%s|%s" % (new, eff, lab, ct, health, res)
        audit_status(st, "mode", key, "[%s] %s" % (new, snap))
    st["mode"] = new
    emit_state(st, lab, ct, health, players, avail, gate_up=gate_up, game_players_known=res_game_players)
    return st

# ---------- Lab-Lebenszyklus-Kommandos ----------
def cmd_start_lab(st, reserve):
    """Lab starten. Zwei Modi, der Unterschied ist der SCHUTZ, nicht mehr das Erzwingen:
       reserve=False (--start-lab): laeuft UNRESERVIERT -> Idle-Auto-Off nach 45min ohne Sitzung.
       reserve=True  (--start-lab --reserve, im dev-portal 'Wartungsmodus'): reservation=lab ->
           kein Auto-Off und keine Verdraengung, solange reserviert.

    Beide pruefen zuerst, ob eine belegte Rolle Vorrang hat. Bis zum 2026-08-23 war --reserve
    der Erzwingen-Modus: er stoppte laufende Spiele, um Platz zu machen. Das ist entfallen:
    ein Wartungsmodus ist eine Ansage fuer die Zukunft ('das hier bitte nicht abraeumen'),
    kein Freibrief, anderen den laufenden Betrieb zu nehmen."""
    lab_now = lab_running()
    block = blocking_role(st, exclude_kind="lab")
    if block:
        audit("[start-lab] ABGELEHNT: %s hat Vorrang, %s" % (role_label(block), absage_grund(block)))
        return
    st["reservation"] = "lab" if reserve else "none"
    for g in GAMES:                  # leere Spiele weichen dem Lab (belegte gibt es hier nicht mehr)
        if game_active(g) and not game_geschuetzt(st, g["name"]):
            audit("[start-lab] leeres Game '%s' graceful stoppen (RAM fuers Win-Lab)" % g["name"])
            game_stop(g); release_game(st, g["name"])
    if mc_ct_state() == "running":
        audit("[start-lab] MC schlafen legen, um RAM fuers Win-Lab freizumachen"); mc_stop()
        if not DRY:
            for _ in range(20):
                time.sleep(3)
                if mc_ct_state() != "running": break
    need = lab_ram_need()
    if not DRY: time.sleep(2)
    avail = free_mb()
    if not DRY and need and avail < need:
        audit("[start-lab] ABGELEHNT: avail %dMB < Lab-Bedarf %dMB (auch mit MC-Schlaf zu wenig -> 32GB-DIMM). Reservation zurueck, Node idle." % (avail, need))
        st["reservation"] = "none"; return
    for vid in CFG["lab_vmids"]:
        if vid in lab_now: audit("[start-lab] VM %d laeuft bereits" % vid); continue
        act("Lab-VM %d starten" % vid, "qm start %d" % vid)
    st["lab_since"] = time.time(); st["lab_idle_since"] = None
    if reserve:
        audit("[start-lab] Win-Lab gestartet + RESERVIERT (Gaming verdraengt; kein Auto-Off; --release -> Idle-Auto-Off; --stop-lab beendet)")
    else:
        audit("[start-lab] Win-Lab gestartet, unreserviert (Idle-Auto-Off nach %ds ohne Sitzung; --stop-lab beendet)" % CFG["lab_idle_timeout_s"])

def cmd_stop_lab(st):
    lab = lab_running()
    if lab: stop_lab_graceful(lab)
    else: audit("[stop-lab] kein Win-Lab laeuft")
    st["reservation"] = "none"; st["lab_since"] = None
    audit("[stop-lab] Win-Lab beendet (graceful) -> Node idle (MC on-demand, nichts weckt automatisch)")

# ---------- Minecraft als on-demand-Rolle (gleichrangig zu den Games) ----------
def cmd_wake_mc(st):
    """MC wecken + reservieren: symmetrisch zu cmd_start_game, nur mit MC-eigenen Start-/Sensor-
    Funktionen (mc_start/mc_ct_state; das Paper-Backend hat eine eigene Health-/Crash-Maschine).
    Eine BELEGTE Rolle (Game/MC mit Spielern ODER reserviertes Lab) hat Vorrang -> ABGELEHNT.
    LEERE Rollen weichen. Setzt reservation=minecraft, verdraengt Lab + laufende Games graceful,
    Precheck-RAM, dann mc_start. Rueckgabe: 'started'|'rejected'|'no-ram'."""
    block = blocking_role(st, exclude_kind="mc")
    if block:
        audit("[wake-mc] ABGELEHNT: %s hat Vorrang, %s" % (role_label(block), absage_grund(block)))
        return "rejected"
    st["reservation"] = "minecraft"
    lab = lab_running()
    if lab:
        audit("[wake-mc] Win-Lab %s graceful verdraengen (RAM fuer MC)" % lab)
        if not stop_lab_graceful(lab):
            audit("[wake-mc] WARN: Lab graceful nicht weg (kein Guest-Agent) -> ggf. --evict-lab; Start evtl. RAM-eng")
    st["lab_since"] = None; st["lab_idle_since"] = None
    for g in GAMES:                           # nur eine schwere Rolle: laufende Games verdraengen
        if game_active(g):
            audit("[wake-mc] anderes Game '%s' laeuft -> graceful stoppen" % g["name"])
            game_stop(g); release_game(st, g["name"])
    if not DRY: time.sleep(2)
    avail = free_mb()
    if not DRY and not precheck_mc(avail):
        audit("[wake-mc] ABGELEHNT: avail %dMB < %dMB. Reservation zurueck." % (avail, CFG["min_free_mb_for_mc"]))
        st["reservation"] = "none"; return "no-ram"
    st["mc_idle_since"] = None; st["mc_unused_since"] = None; st["mc_was_used"] = False
    mc_start()
    audit("[wake-mc] MC geweckt + reserviert (Lab/leere Games weichen). Kommt niemand, geht es nach %ds von selbst wieder aus." % CFG.get("unused_timeout_s", DEFAULT_UNUSED_TIMEOUT_S))
    return "started"

def cmd_sleep_mc(st):
    """MC schlafen legen (on-demand): Backend graceful stoppen + Reservation zurueck -> Node frei.
    Symmetrisch zu cmd_stop_game. Gate bleibt (unabhaengig) oben. Rueckgabe: 'stopped'."""
    if mc_ct_state() == "running":
        mc_stop()
    else:
        audit("[sleep-mc] MC-Backend laeuft nicht")
    st["reservation"] = "none"; st["mc_idle_since"] = None; st["mc_unused_since"] = None
    st["mc_was_used"] = False
    audit("[sleep-mc] MC beendet -> RAM frei, Node idle. Wecken: /minecraft Start bzw. --wake minecraft")
    return "stopped"

# ---------- Generische Game-Lebenszyklus-Kommandos ----------
def _evict_for_ram(st, g, avail):
    """Platz schaffen fuer <g>, indem LEERE, UNGESCHUETZTE Rollen weichen: laengste Leerzeit
    zuerst, damit das am ehesten Vergessene zuerst geht. Belegte Rollen bleiben unangetastet,
    ausnahmslos. Gibt den geschaetzten freien Speicher danach zurueck.

    Das ersetzt die alte Regel 'beim Start eines Spiels weichen ALLE anderen'. Die stammte
    daher, dass auf .18 ohnehin nur eines ins RAM passte, auf dem Spiele-VPS haette sie
    laufende Partien beendet, obwohl reichlich Platz ist."""
    brauch = g.get("min_free_mb", 4000)
    kandidaten = []
    for n in reserved_games(st):
        if n == g.get("name"): continue
        other = game_by_name(n)
        if not other or not game_active(other): continue
        if game_players(other) > 0: continue          # belegt -> bleibt
        if game_geschuetzt(st, n):
            # Reserviert (im Lab: Wartungsmodus), jemand hat ausdruecklich gesagt, dass diese
            # Rolle stehenbleiben soll, auch wenn gerade niemand darauf ist. Genau dafuer gibt
            # es den Schalter: sonst muesste man waehrend der Arbeit dauernd jemanden joinen
            # lassen, damit einem der Server nicht unter den Haenden weggeraeumt wird.
            audit("[start-game:%s] '%s' ist reserviert -> weicht nicht" % (g["name"], n))
            continue
        if int(other.get("idle_timeout_s", 0)) == 0:
            # idle_timeout_s=0 heisst "immer-online", kein Auto-Off, also auch keine
            # Verdraengung (der Auto-Off-Zweig respektierte das seit jeher, der
            # Verdraengungspfad nicht: am 2026-08-22 opferte er prompt DayZ, weil gerade
            # niemand darauf spielte). Seit dem 2026-08-23 nutzt KEIN Spiel mehr diese
            # Markierung: DayZ hat einen Platzhalter bekommen und ist on-demand. Der
            # Mechanismus bleibt fuer den Fall, dass wieder eines dauerhaft laufen soll.
            audit("[start-game:%s] '%s' ist als immer-online markiert -> weicht nicht" % (g["name"], n))
            continue
        slot = game_slot(st, n) or {}
        seit = slot.get("idle_since") or slot.get("unused_since") or slot.get("since") or time.time()
        kandidaten.append((seit, n, other))
    kandidaten.sort()
    for _seit, n, other in kandidaten:
        if avail >= brauch: break
        audit("[start-game:%s] RAM knapp (%dMB < %dMB) -> leeres '%s' weicht" % (g["name"], avail, brauch, n))
        game_stop(other); release_game(st, n)
        avail += game_ram_mb(other)
    return avail

def cmd_start_game(st, name, world=None):
    """Game starten. Seit 2026-08-22 duerfen mehrere gleichzeitig laufen: reicht der freie
    Speicher, weicht NIEMAND. Erst wenn er nicht reicht, weichen LEERE, ungeschuetzte Rollen
    (leere Spiele, schlafendes MC, unreserviertes Lab). Eine BELEGTE Rolle beendet den Versuch
    mit einer Absage, es gibt keinen Override (s. Kopf).
    world (nur multi_world-Games): gewuenschte Welt; laeuft das Game bereits mit einer ANDEREN
    Welt, wird der Start abgelehnt (Wechsel = stop -> wake --world, Admin-Pfad).
    Rueckgabe (fuer wake-bridge/Discord): 'started'|'already-running'|'rejected'|'no-ram'|'unknown'|'bad-world'."""
    if mc_sonderrolle(name):       # eingebaute Maschine; ein Registry-Eintrag gewinnt (s. mc_sonderrolle)
        return cmd_wake_mc(st)
    g = game_by_name(name)
    if not g:
        audit("[start-game] unbekanntes Game '%s' (nicht in games.json). Bekannt: %s" % (name, game_names())); return "unknown"
    w = game_in_wartung(st, name)
    if w:
        # Vor jeder anderen Pruefung: wer eine Wartung gesetzt hat, will nicht, dass dieses
        # Spiel startet. Das gilt auch dann, wenn genug Speicher da waere und niemand sonst
        # spielt. Der Text geht unveraendert bis in Discord und Dashboard durch.
        audit("[start-game:%s] ABGELEHNT: Wartung laeuft. %s. Das Spiel startet erst wieder, "
              "wenn die Wartung beendet ist (arbiter --wartung-aus %s)."
              % (name, wartung_text(w), name))
        return "wartung"
    if world is not None:
        active, ids = world_info(name)
        if not game_multi_world(name) or world not in ids:
            audit("[start-game:%s] ABGELEHNT: unbekannte Welt '%s' (bekannt: %s)" % (name, world, ids)); return "bad-world"
        if game_active(g) and world != active:
            # Ein Welt-Wechsel im laufenden Betrieb wuerde die aktuelle Partie beenden. Das ist
            # ein Stopp, kein Start, und Stoppen ist ein eigener, bewusster Schritt.
            audit("[start-game:%s] ABGELEHNT: laeuft bereits mit Welt '%s'. Wechsel auf '%s' = erst stoppen, "
                  "dann mit der neuen Welt starten (Admin)." % (name, active, world))
            return "rejected"
    if game_active(g) and (world is None or world == world_info(name)[0]):
        # Schon oben. Der Bot ruft /wake auch dann, wenn jemand nur nachsehen will, ein
        # Neustart waere hier das Gegenteil dessen, was gemeint ist.
        reserve_game(st, name)
        audit("[start-game:%s] laeuft bereits -> nichts zu tun (Reservierung bestaetigt)" % name)
        return "already-running"
    if has_role("lab") and st.get("reservation") == "lab":
        # Ein reserviertes Win-Lab ist eine ausdrueckliche Owner-Ansage ('nicht stoeren') und
        # bleibt deshalb unabhaengig vom freien Speicher Vorrang: anders als ein leeres Spiel,
        # das nur RAM belegt. Ohne diese Zeile koennte ein Spiel starten, solange die Lab-VMs
        # noch nicht laufen, und ihnen spaeter den Platz wegnehmen.
        audit("[start-game:%s] ABGELEHNT: das Windows-Lab ist im Wartungsmodus reserviert. "
              "Es bleibt geschuetzt, bis der Wartungsmodus beendet wird." % name)
        return "rejected"
    avail = free_mb()
    if avail >= 0: avail -= booting_reserve_mb(st, exclude=name)
    genug = avail < 0 or avail >= g.get("min_free_mb", 4000)
    if not genug:
        # Erst leere, ungeschuetzte Rollen opfern, dann erneut messen. Belegte bleiben.
        avail = _evict_for_ram(st, g, avail)
        if avail < g.get("min_free_mb", 4000):
            block = blocking_role(st, exclude_kind="game", exclude_name=name)
            if block:
                # Hier endet der Versuch. Frueher stand an dieser Stelle der Hinweis auf
                # --force; ihn zu entfernen ist der eigentliche Sinn dieser Aenderung: eine
                # laufende Partie ist wichtiger als ein Startwunsch.
                audit("[start-game:%s] ABGELEHNT: %dMB frei, %dMB noetig, %s hat Vorrang, %s"
                      % (name, avail, g.get("min_free_mb", 4000), role_label(block), absage_grund(block)))
                return "rejected"
            if has_role("minecraft") and mc_ct_state() == "running":
                audit("[start-game:%s] MC schlafen legen (RAM fuers Game)" % name); mc_stop()
                if not DRY:
                    for _ in range(20):
                        time.sleep(3)
                        if mc_ct_state() != "running": break
            lab = lab_running() if has_role("lab") else []
            if lab:
                audit("[start-game:%s] Win-Lab %s graceful verdraengen (RAM fuers Game)" % (name, lab))
                if not stop_lab_graceful(lab):
                    audit("[start-game:%s] WARN: Lab graceful nicht weg (kein Guest-Agent) -> ggf. --evict-lab; Start evtl. RAM-eng" % name)
                st["lab_since"] = None; st["lab_idle_since"] = None
            if not DRY: time.sleep(2)
            # Der gemessene Wert hinkt dem Stopp hinterher (der Prozess muss erst enden), deshalb
            # gilt der guenstigere von gemessen und rechnerisch-nach-Verdraengung.
            gemessen = free_mb()
            avail = max(avail, gemessen) if gemessen >= 0 else avail
            if not DRY and avail < g.get("min_free_mb", 4000):
                audit("[start-game:%s] ABGELEHNT: %dMB frei, %dMB noetig, %s %s"
                      % (name, avail, g.get("min_free_mb", 4000), ABSAGE_KEIN_RAM, ABSAGE_NACHSATZ))
                return "no-ram"
    if world is not None and not DRY:
        set_active_world(name, world)
    reserve_game(st, name, world=world)
    st["games"][name]["since"] = time.time()   # Start-Fenster fuer die RAM-Buchhaltung
    game_start(g, world=world)
    if int(g.get("idle_timeout_s", 0)) > 0:
        nachsatz = "Kommt niemand, geht es nach %ds von selbst wieder aus." % g.get("unused_timeout_s", DEFAULT_UNUSED_TIMEOUT_S)
    else:
        # Ohne Auto-Off gibt es auch keine Grace-Uhr -- die Zeile behauptete bisher trotzdem
        # eine Frist, die fuer dieses Spiel gar nicht laeuft.
        nachsatz = "Als immer-online markiert: es bleibt an, bis es jemand ausdruecklich stoppt."
    audit("[start-game:%s] gestartet + reserviert (%d Spiel(e) jetzt reserviert). %s"
          % (name, len(reserved_games(st)), nachsatz))
    return "started"

def cmd_stop_game(st, name):
    if mc_sonderrolle(name):       # eingebaute Maschine -> eigener Sleep-Pfad
        return cmd_sleep_mc(st)
    g = game_by_name(name)
    if g and game_active(g): game_stop(g)
    else: audit("[stop-game:%s] laeuft nicht" % name)
    release_game(st, name)
    uebrig = reserved_games(st)
    audit("[stop-game:%s] beendet -> RAM frei (%s)" % (name, ("noch reserviert: " + ", ".join(uebrig)) if uebrig else "nichts mehr reserviert"))

def cmd_restart_game(st, name, world=None):
    """Game neu starten (stop -> kurz warten -> start). Fuer Discord /dayz restart.
    world: optionaler Welt-Wechsel beim Neustart (Admin-Pfad des Dashboards)."""
    if mc_sonderrolle(name):       # eingebaute Maschine -> sleep + wake
        audit("[restart-game:minecraft] -> stop + start")
        cmd_sleep_mc(st)
        if not DRY: time.sleep(3)
        cmd_wake_mc(st); return
    g = game_by_name(name)
    if not g:
        audit("[restart-game] unbekanntes Game '%s' (bekannt: %s)" % (name, game_names())); return
    if world is not None:
        _, ids = world_info(name)
        if world not in ids:
            audit("[restart-game:%s] unbekannte Welt '%s' (bekannt: %s)" % (name, world, ids)); return
    audit("[restart-game:%s] -> stop + start%s" % (name, (" (Welt '%s')" % world) if world else ""))
    if game_active(g): game_stop(g)
    if not DRY: time.sleep(3)
    cmd_start_game(st, name, world=world)

def cmd_adopt(st):
    """Alles, was gerade laeuft, in die Reservierung uebernehmen, ohne einen einzigen Start
    oder Stopp. Der Schritt, mit dem ein Arbiter einen Host uebernimmt, auf dem die Spiele
    schon von Hand laufen: ohne ihn haelt der erste Tick sie fuer Karteileichen und raeumt
    die leeren ab. was_used=True, damit die 20-Minuten-Uhr sofort greift statt der
    Grace-Frist fuer 'geweckt und nie betreten'."""
    uebernommen = []
    for g in GAMES:
        name = g["name"]
        if not game_active(g):
            continue
        if game_reserved(st, name):
            uebernommen.append(name + " (war schon reserviert)"); continue
        reserve_game(st, name, was_used=True)
        st["games"][name]["since"] = None      # laeuft laengst -> kein Start-Fenster
        p = game_players(g) if probe_counts_players(g) else -1
        uebernommen.append("%s (%s)" % (name, ("%d Spieler" % p) if p >= 0 else "Spielerzahl unbekannt"))
    if uebernommen:
        audit("[adopt] uebernommen: %s" % ", ".join(uebernommen))
    else:
        audit("[adopt] nichts Laufendes gefunden, keine Reservierung angelegt")
    return uebernommen

def cmd_probe(name):
    """Read-only Readiness-Snapshot EINES Games/MC fuer den Discord-Ladebalken (via wake-bridge
    GET /ready/<game>). Kein State, kein flock -> darf parallel zum Tick laufen (wie --list-games).
    Liefert die Start-Phasen 'lxc_running' -> 'service_active' -> 'reachable' (= Server joinbar)
    + echte Spielerzahl, sobald messbar. minecraft ist eine gleichrangige on-demand-Rolle mit
    eigener Health-/Sensor-Maschine -> Sonderpfad."""
    if mc_sonderrolle(name):
        up = mc_lxc_running(); ct = mc_ct_state() if up else "absent"
        health = mc_health() if ct == "running" else "n/a"
        players = mc_players() if ct == "running" else -1
        return {"game": "minecraft", "lxc_running": up, "service_active": ct == "running",
                "reachable": ct == "running" and health == "healthy",
                "players": (players if players >= 0 else None), "health": health}
    g = game_by_name(name)
    if not g:
        return {"game": name, "error": "unknown", "known": game_names()}
    up = game_host_ready(g)
    active = game_active(g) if up else False
    reachable = game_reachable(g) if active else False
    players = game_players(g) if reachable else -1
    return {"game": name, "lxc_running": up, "service_active": active,
            "reachable": reachable, "players": (players if players >= 0 else None)}

def main():
    global DRY, _SIM_LAB
    # Die Abweisung von --force steht VOR jedem Seiteneffekt (kein makedirs, kein Lock, kein
    # State-Schreiben): eine Fehlbedienung soll nichts anfassen, auch nicht das Verzeichnis.
    if "--force" in sys.argv:
        # Bewusst eine laute Absage statt stillem Ignorieren: --force steckt moeglicherweise noch
        # in einem Skript, einer Notiz oder im Muskelgedaechtnis. Wer es benutzt, soll erfahren,
        # dass es den Override nicht mehr gibt, nicht denken, er habe gewirkt.
        print("--force gibt es nicht mehr: wer spielt, wird nicht verdraengt. Ohne das Flag "
              "erneut aufrufen: leere, ungeschuetzte Rollen weichen weiterhin von selbst.",
              file=sys.stderr)
        sys.exit(2)
    os.makedirs(BASE, exist_ok=True)
    DRY = "--live" not in sys.argv
    confirm_evict = "--confirm-evict" in sys.argv
    confirm_hard = "--confirm-hard-evict" in sys.argv
    if "--simulate-lab" in sys.argv: _SIM_LAB = CFG["lab_vmids"]
    for i, a in enumerate(sys.argv):
        if a == "--min-free" and i+1 < len(sys.argv): CFG["min_free_mb_for_mc"] = int(sys.argv[i+1])
    # --list-games ist rein lesend (Modul-Registry, kein State) -> VOR dem flock beantworten. Sonst
    # konkurriert die wake-bridge (known_games ruft --list-games) mit einem laufenden Tick um den Lock
    # -> sporadisch leere Liste -> '/wake/<game>' faelschlich 'unknown game'. minecraft ist gleichrangige
    # on-demand-Rolle -> mit auflisten (Bridge validiert /wake|/sleep|/restart/<g> hiergegen).
    if "--list-games" in sys.argv:
        mc_entry = [{"name": "minecraft", "ctid": CFG["mc_ctid"], "kind": "mc", "idle_timeout_s": CFG["idle_timeout_s"]}] if has_role("minecraft") else []
        print(json.dumps(mc_entry + [{"name": g["name"], "ctid": g.get("ctid"), "kind": g.get("kind"), "idle_timeout_s": g.get("idle_timeout_s")} for g in GAMES], indent=2)); return
    # --list-worlds <game> ist rein lesend (worlds.json) -> ebenfalls VOR dem flock.
    for i, a in enumerate(sys.argv):
        if a == "--list-worlds" and i+1 < len(sys.argv):
            nm = sys.argv[i+1]
            active, ids = world_info(nm)
            print(json.dumps({"game": nm, "multi_world": game_multi_world(nm),
                              "active": active, "worlds": ids})); return
    # --probe <game> ist rein lesend (Readiness-Snapshot) -> ebenfalls VOR dem flock, damit der
    # Discord-Ladebalken (pollt /ready/<game>) nicht mit einem laufenden Tick um den Lock kaempft.
    for i, a in enumerate(sys.argv):
        if a == "--probe" and i+1 < len(sys.argv):
            print(json.dumps(cmd_probe(sys.argv[i+1]))); return
    # --list-snapshots <game> ist rein lesend (Dateisystem) -> ebenfalls VOR dem flock.
    for i, a in enumerate(sys.argv):
        if a == "--list-snapshots" and i+1 < len(sys.argv):
            print(json.dumps(cmd_list_snapshots(sys.argv[i+1]))); return
    lockf = open(LOCK_FILE, "w")
    # Lock-Politik: Ein state-aenderndes KOMMANDO (wake/sleep/restart/reserve/start/stop/...) darf
    # NICHT still verpuffen, wenn es zufaellig mit dem periodischen 60s-Tick um den Lock kollidiert
    # (frueher: exit 0 ohne Aktion -> Discord-„wird gestartet", aber der Server startete nie). Solche
    # Kommandos WARTEN daher bis zu 45s auf den Lock. Der Tick selbst (kein Kommando-Flag) bricht bei
    # belegtem Lock wie bisher ab -> der naechste Tick kommt in 60s.
    _CMD_FLAGS = {"--adopt", "--reserve-lab", "--release", "--reset", "--start-mc", "--stop-mc", "--start-lab",
                  "--stop-lab", "--start-dayz", "--stop-dayz", "--reserve-dayz", "--start-game", "--wake",
                  "--stop-game", "--sleep", "--restart-game", "--restart", "--reserve-game", "--evict-lab",
                  "--reservieren", "--freigeben", "--wartung-an", "--wartung-aus",
                  "--status", "--test-precheck", "--create-world",
                  "--snapshot", "--snapshot-all", "--restore", "--delete-snapshot", "--delete-world"}
    is_command = any(a in _CMD_FLAGS for a in sys.argv)
    if is_command:
        deadline = time.time() + 45
        while True:
            try:
                fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB); break
            except BlockingIOError:
                if time.time() >= deadline:
                    print("Lock nach 45s nicht frei -- Kommando abgebrochen"); sys.exit(1)
                time.sleep(0.5)
    else:
        try: fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: print("Orchestrator laeuft bereits (flock) -- Abbruch"); sys.exit(0)

    st = load_state()
    if "--adopt" in sys.argv:
        cmd_adopt(st); save_state(st); return
    for _flag, _rolle in (("--start-mc", "minecraft"), ("--stop-mc", "minecraft"),
                          ("--start-lab", "lab"), ("--stop-lab", "lab"),
                          ("--reserve-lab", "lab"), ("--evict-lab", "lab")):
        if _flag in sys.argv and not has_role(_rolle):
            print("%s gibt es an diesem Ort nicht (Profil '%s': roles.%s=false)" % (_flag, PROFILE["node"], _rolle))
            sys.exit(1)
    if "--reserve-lab" in sys.argv: st["reservation"]="lab"; save_state(st); audit("[cmd] reservation=lab (Lab geschuetzt, auto-off aus)"); return
    if "--release" in sys.argv:     st["reservation"]="none"; save_state(st); audit("[cmd] reservation=none (Lab wieder auto-off-faehig)"); return
    if "--reset" in sys.argv:       st.update(mode="IDLE", restart_count=0); save_state(st); audit("[cmd] FAILED-Reset -> IDLE"); return
    # --start-mc/--stop-mc = MC als on-demand-Rolle wecken/schlafen (Aliase zu --wake/--sleep minecraft;
    # so treiben wake-bridge /mc/start|/mc/stop + Herb /minecraft den neuen on-demand-MC unveraendert an).
    if "--start-mc" in sys.argv:    r=cmd_wake_mc(st); save_state(st); sys.exit(0 if r in START_ERFOLG else 3)
    if "--stop-mc" in sys.argv:     cmd_sleep_mc(st); save_state(st); return
    if "--start-lab" in sys.argv:   cmd_start_lab(st, "--reserve" in sys.argv); save_state(st); return
    if "--stop-lab" in sys.argv:    cmd_stop_lab(st); save_state(st); return
    if "--start-dayz" in sys.argv:  r=cmd_start_game(st, "dayz"); save_state(st); sys.exit(0 if r in START_ERFOLG else 3)   # Alias
    if "--stop-dayz" in sys.argv:   cmd_stop_game(st, "dayz"); save_state(st); return
    if "--reserve-dayz" in sys.argv:
        reserve_game(st, "dayz")
        save_state(st); audit("[cmd] dayz in den Slot eingetragen (Start beim naechsten Tick)"); return
    # Schutz an/aus ("Reservieren" im Dashboard, "Wartungsmodus" bei Lab-Diensten): das Spiel
    # bleibt stehen, bis der Schutz faellt, kein Auto-Off, keine Verdraengung. Bewusst getrennt
    # von --reserve-game, das nur den Slot anlegt und damit einen Start ausloest.
    # Wartung an/aus: "dieses Spiel darf gerade nicht starten". Gedacht fuer Arbeit AM Spiel
    # (Update, Mods, Weltpflege) und damit das Gegenstueck zu --reservieren, das ein laufendes
    # Spiel stehen laesst. Beides zusammen deckt die zwei Faelle ab, in denen der Automatik
    # nicht zu trauen ist: "fass das Laufende nicht an" und "lass das Schlafende schlafen".
    for i, a in enumerate(sys.argv):
        if a in ("--wartung-an", "--wartung-aus") and i+1 < len(sys.argv):
            an = (a == "--wartung-an"); nm = sys.argv[i+1]
            if not game_by_name(nm):
                audit("[cmd] unbekanntes Game '%s' (bekannt: %s)" % (nm, game_names())); sys.exit(1)
            grund = ""
            for j, b in enumerate(sys.argv):
                if b == "--grund" and j+1 < len(sys.argv): grund = sys.argv[j+1]
            vorher = game_in_wartung(st, nm)
            set_game_wartung(st, nm, an, grund)
            save_state(st)
            if an:
                audit("[cmd] %s in Wartung: %s. Weckversuche werden bis zum Ende der Wartung "
                      "abgelehnt, der Platzhalter laeuft weiter." % (nm, grund or "Wartung"))
            elif vorher:
                audit("[cmd] %s aus der Wartung entlassen (%s) -> wieder weckbar"
                      % (nm, wartung_text(vorher)))
            else:
                audit("[cmd] %s war gar nicht in Wartung -> nichts zu tun" % nm)
            return
    for i, a in enumerate(sys.argv):
        if a in ("--reservieren", "--freigeben") and i+1 < len(sys.argv):
            an = (a == "--reservieren"); nm = sys.argv[i+1]
            if not game_by_name(nm):
                audit("[cmd] unbekanntes Game '%s' (bekannt: %s)" % (nm, game_names())); sys.exit(1)
            if not set_game_geschuetzt(st, nm, an):
                audit("[cmd] '%s' wird gerade nicht verwaltet (laeuft nicht) -> nichts zu reservieren" % nm)
                sys.exit(3)
            save_state(st)
            status_schutz_nachziehen(st)   # sonst zeigt die Oberflaeche bis zum naechsten Tick den alten Stand
            audit("[cmd] %s %s" % (nm, "reserviert (kein Auto-Off, wird nicht verdraengt)" if an
                                   else "freigegeben (Auto-Off und Verdraengung wieder moeglich)"))
            return
    # Generische Game-Control-API (Trigger fuer Discord/netcup-Wake in Phase 3):
    # --world <id>: optionale Welt fuer --wake/--restart-game (nur multi_world-Games).
    world = None
    for i, a in enumerate(sys.argv):
        if a == "--world" and i+1 < len(sys.argv): world = sys.argv[i+1]
    wlabel = None
    for i, a in enumerate(sys.argv):
        if a == "--world-label" and i+1 < len(sys.argv): wlabel = sys.argv[i+1]
    for i, a in enumerate(sys.argv):
        if a == "--create-world" and i+2 < len(sys.argv):
            r = cmd_create_world(sys.argv[i+1], sys.argv[i+2], wlabel)
            sys.exit(0 if r == "created" else 3)
        if a in ("--start-game", "--wake") and i+1 < len(sys.argv):
            r = cmd_start_game(st, sys.argv[i+1], world=world); save_state(st)
            sys.exit(0 if r in START_ERFOLG else 3)
        if a in ("--stop-game", "--sleep") and i+1 < len(sys.argv): cmd_stop_game(st, sys.argv[i+1]); save_state(st); return
        if a in ("--restart-game", "--restart") and i+1 < len(sys.argv): cmd_restart_game(st, sys.argv[i+1], world=world); save_state(st); return
        if a == "--reserve-game" and i+1 < len(sys.argv):
            nm = sys.argv[i+1]
            if mc_sonderrolle(nm):
                st.update(reservation="minecraft", mc_idle_since=None, mc_unused_since=None, mc_was_used=False)
                save_state(st); audit("[cmd] reservation=minecraft (Lab/Games weichen; Start beim naechsten Tick)")
            elif game_by_name(nm):
                reserve_game(st, nm)
                save_state(st); audit("[cmd] %s reserviert (Start beim naechsten Tick)" % nm)
            else: audit("[cmd] unbekanntes Game '%s' (bekannt: %s)" % (nm, game_names()))
            return
    # Snapshot-/Restore-/Loesch-Kommandos (Dashboard-/Owner-Pfad via wake-bridge bzw.
    # Nightly-Hook). Exit-Codes: 0 ok, 1 Fehler, 4 Spiel laeuft/geschuetzt, 5 unbekannt.
    _snap_cmds = ("--snapshot", "--snapshot-all", "--restore", "--delete-snapshot", "--delete-world")
    if any(a in _snap_cmds for a in sys.argv):
        if DRY:
            print("DRY-RUN: Snapshot-/Restore-Kommandos brauchen --live"); sys.exit(1)
        if "--snapshot-all" in sys.argv:
            sys.exit(cmd_snapshot_all())
        for i, a in enumerate(sys.argv):
            if a == "--snapshot" and i+1 < len(sys.argv):
                sys.exit(cmd_snapshot(sys.argv[i+1], world=world))
            if a == "--restore" and i+2 < len(sys.argv):
                sys.exit(cmd_restore(st, sys.argv[i+1], sys.argv[i+2]))
            if a == "--delete-snapshot" and i+2 < len(sys.argv):
                sys.exit(cmd_delete_snapshot(sys.argv[i+1], sys.argv[i+2]))
            if a == "--delete-world" and i+2 < len(sys.argv):
                sys.exit(cmd_delete_world(st, sys.argv[i+1], sys.argv[i+2]))
        sys.exit(5)
    if "--evict-lab" in sys.argv:
        lab = lab_running()
        if not lab: audit("[evict-lab] kein Win-Lab laeuft")
        elif not confirm_evict: audit("[evict-lab] braucht --confirm-evict (+ optional --confirm-hard-evict fuer Hard-Kill bei Timeout)")
        else:
            ok = evict_lab(lab, confirm_hard); st["reservation"]="none"; st["lab_since"]=None
            audit("[evict-lab] %s" % ("Lab weg -> RAM frei" if ok else "unvollstaendig"))
        save_state(st); return
    if "--status" in sys.argv:
        print(json.dumps(st, indent=2))
        if os.path.exists(STATUS_FILE):
            with open(STATUS_FILE) as f: print("--- live ---"); print(f.read())
        return
    if "--test-precheck" in sys.argv:
        avail = free_mb(); ok = precheck_mc(avail)
        audit("[test-precheck] avail %dMB, MC-Start %s" % (avail, "ERLAUBT" if ok else "ABGELEHNT")); return

    st = tick(st, confirm_evict, confirm_hard); save_state(st)

if __name__ == "__main__":
    main()
