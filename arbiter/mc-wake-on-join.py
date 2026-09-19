#!/usr/bin/env python3
"""mc-wake-on-join, start-on-join fuer den on-demand Minecraft-Server (LXC-Fassung, Node .18).

⚠️ **Das ist NICHT die laufende Fassung.** Minecraft liegt seit dem 2026-08-22 host-nativ
auf gamehost, und dort laeuft `minecraft-server/gamehost/mc-wake-on-join.py` aus
`/opt/mc-helfer/`. Diese Datei hier ist die aeltere LXC-203-Variante (`pct exec`) und wird
seit dem 2026-09-11 nur noch ausgerollt, wenn die `arbiter.json` des Ziels
`roles.minecraft` auf true stehen hat. Auf .18 steht sie auf false, LXC 203 ist geloescht.

Wer den Weckvorgang aendern will, aendert die gamehost-Fassung. Sie hat unter anderem eine
Whitelist-Pruefung, die es hier nicht gibt.

Warum: Der oeffentliche MC-Endpoint laeuft ueber Velocity 4 + Minekube Connect (greenleaf.play.minekube.net).
Velocity haelt den Tunnel dauerhaft; NanoLimbo (Warteraum) haelt den Spieler VERBUNDEN, waehrend das
on-demand Paper-Backend (mc-poc) schlaeft. Weckt aber kein Backend von selbst -> dieser Daemon folgt
den Velocity-Logs und triggert bei einem ECHTEN Login die wake-bridge (/mc/start). Das LimboWake-Plugin
im Velocity holt den bereits im Limbo wartenden Spieler dann nahtlos ins survival, sobald es hochkam.

Deterministische Erkennung (Velocity, log-player-connections=true):
  * ECHTER Login  -> '[connected player] <name> (/ip:port) has connected'  (nur bei echtem Spieler-Login)
  * Serverlisten-Ping / Connect-Edge-Healthcheck -> erzeugen KEIN '[connected player]' -> wecken NICHT
  (Historie: gegen Gate Lite waren es 'handshakeSession.lite ... failed to try backend'-Muster.)

Sicherheit / Robustheit:
  * Nutzt /mc/start -> ein BESPIELTES oder reserviertes Game/Lab hat Vorrang (Arbiter 'rejected');
    MC verdraengt NIE ein laufendes Spiel (Arbiter blocking_role). Einen Override gibt es
    ueberhaupt nicht mehr, auch nicht fuer Admins (2026-08-23).
  * Cooldown zwischen Wakes (kein Wake-Sturm; Paper-Boot dauert ~30-70s).
  * Skip, wenn mc-poc bereits laeuft (Velocity reicht dann direkt durch -> kein Wake noetig).
  * Reconnect-Loop um 'docker logs -f' (ueberlebt Velocity-/LXC-Restart) + systemd Restart=always.

Env: MC_CTID(203) GATE(mc-velocity) MC_BACKEND(mc-poc)
     WAKE_URL(http://127.0.0.1:8129/mc/start) TOKEN_FILE(/opt/game-arbiter/wake.token)
     COOLDOWN_S(120) DRY(0)
"""
import os, sys, time, subprocess, urllib.request

CTID       = os.environ.get("MC_CTID", "203")
GATE       = os.environ.get("GATE", "mc-velocity")   # Proxy, dessen Logs wir folgen (seit Weg B: Velocity 4)
MC_BACKEND = os.environ.get("MC_BACKEND", "mc-poc")
WAKE_URL   = os.environ.get("WAKE_URL", "http://127.0.0.1:8129/mc/start")
TOKEN_FILE = os.environ.get("TOKEN_FILE", "/opt/game-arbiter/wake.token")
COOLDOWN   = int(os.environ.get("COOLDOWN_S", "120"))
DRY        = os.environ.get("DRY", "0") == "1"


def log(msg):
    print("[wake-on-join] " + msg, flush=True)


def token():
    try:
        with open(TOKEN_FILE) as f:
            return f.read().strip()
    except Exception:
        return None


def mc_running():
    """True wenn das Paper-Backend laeuft (dann kein Wake noetig -- Gate reicht durch)."""
    try:
        r = subprocess.run(
            ["pct", "exec", CTID, "--", "docker", "inspect", "-f", "{{.State.Running}}", MC_BACKEND],
            capture_output=True, text=True, timeout=15)
        return r.stdout.strip() == "true"
    except Exception:
        return False


def is_join_attempt(line):
    """Nur ein ECHTER Spieler-Login am Proxy soll wecken. Velocity loggt bei
    log-player-connections=true genau eine Zeile pro echtem Login:
      '[connected player] <name> (/ip:port) has connected'
    Serverlisten-Pings + Connect-Edge-Healthchecks erzeugen KEINE solche Zeile -> kein Wake."""
    return "[connected player]" in line and "has connected" in line


def wake():
    tok = token()
    if not tok:
        log("KEIN Token (%s) -> kann nicht wecken" % TOKEN_FILE)
        return
    if DRY:
        log("DRY-RUN: wuerde POST %s ausloesen (kein echter Wake)" % WAKE_URL)
        return
    try:
        req = urllib.request.Request(WAKE_URL, method="POST",
                                     headers={"Authorization": "Bearer " + tok})
        with urllib.request.urlopen(req, timeout=185) as resp:
            body = resp.read().decode("utf-8", "replace")[:300]
        log("wake ausgeloest -> HTTP %s %s" % (resp.status, body))
    except Exception as e:
        log("wake-Request Fehler: %s" % e)


def follow():
    """Folgt den Gate-Logs (nur neue Zeilen); yieldet erkannte Join-Versuche."""
    cmd = ["pct", "exec", CTID, "--", "docker", "logs", "-f", "--tail", "0", GATE]
    log("folge Velocity-Logs: %s" % " ".join(cmd))
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, bufsize=1)
    try:
        for line in p.stdout:
            if is_join_attempt(line):
                yield line.rstrip()
    finally:
        try:
            p.terminate()
        except Exception:
            pass


def main():
    log("start (ctid=%s gate=%s backend=%s wake=%s cooldown=%ds dry=%s)"
        % (CTID, GATE, MC_BACKEND, WAKE_URL, COOLDOWN, DRY))
    last = 0.0
    while True:
        try:
            for line in follow():
                now = time.time()
                if now - last < COOLDOWN:
                    log("Join erkannt, aber Cooldown aktiv (%ds Rest) -> skip"
                        % int(COOLDOWN - (now - last)))
                    continue
                if mc_running():
                    log("Join erkannt, aber mc-poc laeuft bereits -> kein Wake noetig")
                    last = now
                    continue
                log("ECHTER Join-Versuch erkannt -> wecke MC")
                log("  velocity: " + line[:200])
                wake()
                last = now
        except Exception as e:
            log("follow-Fehler: %s -> reconnect in 5s" % e)
        time.sleep(5)   # Backoff bei Stream-Ende / Fehler


if __name__ == "__main__":
    main()
