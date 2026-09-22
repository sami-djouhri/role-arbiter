#!/usr/bin/env python3
"""wake-bridge, schlanker HTTP-Trigger fuer den Game-Arbiter (Node .18).

Erlaubt Discord-Bot / dev-portal-Button / CLI, on-demand Games zu wecken/schlafen zu legen,
OHNE SSH-Node-Zugang zu verteilen. Der eigentliche privilegierte Vorgang (pct/qm) bleibt im
Arbiter; die Bridge ruft nur 'arbiter.py --live --wake/--sleep <game>'.

Sicherheit:
  * Gebunden an die LAN-IP (default 192.0.2.10), NICHT 0.0.0.0 -> netcup/public kommt nicht ran.
  * Bearer-Token (Datei /opt/game-arbiter/wake.token, chmod 600). Ohne gueltigen Token -> 401.
  * Nur bekannte Games (arbiter --list-games) sind weckbar; alles andere -> 400.
  * Kein Shell-Injection: game-Name gegen die Registry validiert, subprocess ohne shell.

Endpunkte:
  POST /wake/<game>[?world=<id>]  Bearer <token>  -> Game wecken; world = Welt-Auswahl (multi_world).
                                         rc=3 = abgelehnt (belegte/reservierte Rolle hat Vorrang
                                         oder kein Speicher). Das fruehere ?force=1 gibt es NICHT
                                         mehr: wer spielt, wird nicht verdraengt (2026-08-23).
  POST /sleep/<game>  Bearer <token>  -> Game schlafen legen (RAM frei)
  POST /restart/<game>[?world=<id>]  Bearer <token>  -> Neustart, optional mit Welt-Wechsel
  POST /reservieren/<game> | /freigeben/<game>  Bearer <token>  -> Schutz an/aus: kein Auto-Off,
                                         keine Verdraengung. rc=3 = Spiel laeuft gerade nicht.
  POST /lab/start[?wartung=1] | /lab/stop | /lab/wartung?an=0|1  Bearer <token>  -> Windows-AD-Lab
                                         (nur dort, wo es die Rolle gibt: Node .18). wartung=1 =
                                         reserviert starten (kein Auto-Off, keine Verdraengung).
  POST /worlds/<game>/create  Bearer <token>, JSON {"id","label"}  -> Welt in der Registry anlegen
                                         (Dateien entstehen lazy beim ersten Start)
  POST /worlds/<game>/delete  Bearer <token>, JSON {"id"}  -> Welt loeschen (Arbiter schuetzt
                                         aktive/letzte Welt, macht vorher einen Abschieds-Snapshot)
  POST /snapshot/<game>[?world=<id>]  Bearer <token>  -> Welt-Snapshot jetzt erstellen (manual/)
  POST /restore/<game>   Bearer <token>, JSON {"file"}  -> Snapshot zurueckspielen (nur bei
                                         gestopptem Spiel, rc=4 sonst; prerestore-Sicherung vorher)
  POST /snapshots/<game>/delete  Bearer <token>, JSON {"file"}  -> Snapshot-Datei loeschen
  GET  /status                        -> live status.json (kein Token noetig, read-only)
  GET  /worlds/<game>                 -> Welt-Registry eines Games (kein Token, read-only, live)
  GET  /snapshots/<game>              -> Snapshot-Liste (nightly+manual; kein Token, read-only)
  GET  /ready/<game>                  -> Readiness-Snapshot fuers Discord-Ladebalken-Polling
                                         (lxc_running/service_active/reachable/players; kein Token, read-only)

Env: WAKE_BIND (default 192.0.2.10), WAKE_PORT (default 8129), ARBITER (default /opt/game-arbiter/arbiter.py).
"""
import json, os, re, subprocess, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

WORLD_ID_RE = re.compile(r"^[a-z0-9-]{3,24}$")
# Snapshot-Dateien wie von --list-snapshots geliefert (relativ zu /var/backups/game-saves);
# strengere Pruefung (Existenz, Game-Zuordnung) macht der Arbiter selbst.
SNAP_FILE_RE = re.compile(r"^[a-z0-9][A-Za-z0-9._/-]{0,120}\.tar\.gz$")

BIND    = os.environ.get("WAKE_BIND", "192.0.2.10")
PORT    = int(os.environ.get("WAKE_PORT", "8129"))
ARBITER = os.environ.get("ARBITER", "/opt/game-arbiter/arbiter.py")
BASE    = os.path.dirname(ARBITER)
TOKEN_FILE  = BASE + "/wake.token"
STATUS_FILE = BASE + "/status.json"

def load_token():
    try:
        with open(TOKEN_FILE) as f: return f.read().strip()
    except Exception: return None

def known_games():
    try:
        out = subprocess.check_output([sys.executable, ARBITER, "--list-games"], timeout=10, text=True)
        return [g["name"] for g in json.loads(out)]
    except Exception: return []

class H(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        try:
            self.send_response(code); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body))); self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionError):
            pass   # Client weg (z.B. terraria-greeter, den der ausgeloeste Wake selbst stoppte) -> Antwort verpufft, ok
    def log_message(self, *a): pass   # kein Request-Spam ins journal
    def _authed(self):
        tok = load_token()
        if not tok: return False
        h = self.headers.get("Authorization", "")
        return h == "Bearer " + tok
    def do_GET(self):
        if self.path == "/status":
            try:
                with open(STATUS_FILE) as f: self._send(200, json.load(f))
            except Exception: self._send(200, {"status": "unknown"})
        elif self.path.startswith("/worlds/"):
            # Welt-Registry EINES Games (token-frei, read-only wie /status; live via
            # --list-worlds, damit frisch angelegte Welten sofort sichtbar sind).
            game = self.path[len("/worlds/"):].strip("/")
            if game not in known_games():
                return self._send(400, {"error": "unknown game", "known": known_games()})
            try:
                out = subprocess.check_output([sys.executable, ARBITER, "--list-worlds", game], timeout=10, text=True)
                self._send(200, json.loads(out))
            except Exception as e:
                self._send(200, {"game": game, "error": str(e)})
        elif self.path.startswith("/snapshots/"):
            # Snapshot-Liste EINES Games (token-frei, read-only wie /status/worlds).
            game = self.path[len("/snapshots/"):].strip("/")
            if game not in known_games():
                return self._send(400, {"error": "unknown game", "known": known_games()})
            try:
                out = subprocess.check_output([sys.executable, ARBITER, "--list-snapshots", game], timeout=15, text=True)
                self._send(200, json.loads(out))
            except Exception as e:
                self._send(200, {"game": game, "error": str(e), "snapshots": []})
        elif self.path.startswith("/ready/"):
            # Readiness-Probe EINES Games fuer den Discord-Ladebalken (token-frei, read-only wie
            # /status). Ruft 'arbiter.py --probe <game>' (rein lesend, VOR flock) -> Start-Phasen
            # lxc_running/service_active/reachable + Spielerzahl. Nur bekannte Games (kein Injection).
            game = self.path[len("/ready/"):].strip("/")
            if game not in known_games():
                return self._send(400, {"error": "unknown game", "known": known_games()})
            try:
                out = subprocess.check_output([sys.executable, ARBITER, "--probe", game], timeout=25, text=True)
                self._send(200, json.loads(out))
            except Exception as e:
                self._send(200, {"game": game, "reachable": False, "error": str(e)})
        else:
            self._send(404, {"error": "not found"})
    def _run(self, label, *flags, timeout=180):
        try:
            r = subprocess.run([sys.executable, ARBITER, "--live", *flags],
                               capture_output=True, text=True, timeout=timeout)
            self._send(200, {"action": label, "rc": r.returncode, "log": (r.stdout or "").splitlines()[-6:]})
        except subprocess.TimeoutExpired:
            self._send(504, {"error": "arbiter timeout", "action": label})
    def _json_body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            return None
    def do_POST(self):
        parsed = urlparse(self.path)
        parts  = parsed.path.strip("/").split("/")
        if not self._authed():
            return self._send(401, {"error": "invalid or missing bearer token"})
        # Minecraft (on-demand-Rolle wie jedes Game): /mc/start | /mc/stop = --start-mc/--stop-mc
        # (= --wake/--sleep minecraft). Alternativ auch generisch via /wake/minecraft (minecraft ist
        # in --list-games). rc==3 = Konflikt (belegte Rolle hat Vorrang) -> Bot zeigt Erzwingen-Button.
        if len(parts) == 2 and parts[0] == "mc" and parts[1] in ("start", "stop"):
            return self._run("mc/" + parts[1], "--start-mc" if parts[1] == "start" else "--stop-mc")
        # Windows-AD-Lab: /lab/start[?wartung=1] | /lab/stop | /lab/wartung?an=0|1
        # Bis 2026-08-23 bewusst NICHT exponiert, weil '--start-lab --reserve' damals der
        # Erzwingen-Modus war: ein Klick im Web haette laufende Spiele beendet. Dieser Modus
        # ist entfallen, ein Lab-Start weicht heute nur leeren, ungeschuetzten Rollen und
        # wird sonst abgelehnt (rc=0 mit ABGELEHNT-Zeile im Log). Damit ist der Weg
        # ungefaehrlich genug fuer einen Knopf im dev-portal.
        if len(parts) == 2 and parts[0] == "lab":
            if parts[1] == "start":
                wartung = parse_qs(parsed.query).get("wartung", ["0"])[0].lower() in ("1", "true", "yes")
                extra = ["--reserve"] if wartung else []
                return self._run("lab/start", "--start-lab", *extra, timeout=300)
            if parts[1] == "stop":
                return self._run("lab/stop", "--stop-lab", timeout=300)
            if parts[1] == "wartung":
                an = parse_qs(parsed.query).get("an", ["1"])[0].lower() in ("1", "true", "yes")
                # --reserve-lab schuetzt das Lab (kein Auto-Off, keine Verdraengung),
                # --release gibt es wieder frei. Beide starten und stoppen NICHTS.
                return self._run("lab/wartung", "--reserve-lab" if an else "--release")
        # Welt anlegen: /worlds/<game>/create, Body JSON {"id": "...", "label": "..."}
        if len(parts) == 3 and parts[0] == "worlds" and parts[2] == "create":
            game = parts[1]
            if game not in known_games():
                return self._send(400, {"error": "unknown game", "known": known_games()})
            body = self._json_body()
            if body is None:
                return self._send(400, {"error": "invalid json body"})
            wid = str(body.get("id") or "").strip()
            if not WORLD_ID_RE.match(wid):
                return self._send(400, {"error": "invalid world id (a-z0-9-, 3-24)"})
            label = str(body.get("label") or "").strip()[:40]
            extra = ["--world-label", label] if label else []
            return self._run("worlds/%s/create" % game, "--create-world", game, wid, *extra)
        # Welt loeschen: /worlds/<game>/delete, Body JSON {"id"}, Arbiter schuetzt aktive/letzte
        # Welt (rc=4) und legt vorher einen 'deleted-'-Abschieds-Snapshot an.
        if len(parts) == 3 and parts[0] == "worlds" and parts[2] == "delete":
            game = parts[1]
            if game not in known_games():
                return self._send(400, {"error": "unknown game", "known": known_games()})
            body = self._json_body()
            wid = str((body or {}).get("id") or "").strip()
            if not WORLD_ID_RE.match(wid):
                return self._send(400, {"error": "invalid world id (a-z0-9-, 3-24)"})
            return self._run("worlds/%s/delete" % game, "--delete-world", game, wid, timeout=300)
        # Snapshot jetzt: /snapshot/<game>[?world=<id>] -> manual/<game>/<welt>-<ts>.tar.gz
        if len(parts) == 2 and parts[0] == "snapshot":
            game = parts[1]
            if game not in known_games():
                return self._send(400, {"error": "unknown game", "known": known_games()})
            world = parse_qs(parsed.query).get("world", [""])[0]
            extra = []
            if world:
                if not WORLD_ID_RE.match(world):
                    return self._send(400, {"error": "invalid world id (a-z0-9-, 3-24)"})
                extra = ["--world", world]
            return self._run("snapshot/" + game, "--snapshot", game, *extra, timeout=600)
        # Restore: /restore/<game>, Body JSON {"file"}, nur bei gestopptem Spiel (rc=4 sonst).
        if len(parts) == 2 and parts[0] == "restore":
            game = parts[1]
            if game not in known_games():
                return self._send(400, {"error": "unknown game", "known": known_games()})
            body = self._json_body()
            fn = str((body or {}).get("file") or "").strip()
            if ".." in fn or not SNAP_FILE_RE.match(fn):
                return self._send(400, {"error": "invalid snapshot file"})
            return self._run("restore/" + game, "--restore", game, fn, timeout=600)
        # Snapshot loeschen: /snapshots/<game>/delete, Body JSON {"file"}
        if len(parts) == 3 and parts[0] == "snapshots" and parts[2] == "delete":
            game = parts[1]
            if game not in known_games():
                return self._send(400, {"error": "unknown game", "known": known_games()})
            body = self._json_body()
            fn = str((body or {}).get("file") or "").strip()
            if ".." in fn or not SNAP_FILE_RE.match(fn):
                return self._send(400, {"error": "invalid snapshot file"})
            return self._run("snapshots/%s/delete" % game, "--delete-snapshot", game, fn)
        # Games: /wake/<game>[?world=..] | /sleep/<game> | /restart/<game>[?world=..]
        if len(parts) == 2 and parts[0] in ("wake", "sleep", "restart"):
            game = parts[1]
            if game not in known_games():
                return self._send(400, {"error": "unknown game", "known": known_games()})
            flag  = {"wake": "--wake", "sleep": "--sleep", "restart": "--restart-game"}[parts[0]]
            extra = []
            world = parse_qs(parsed.query).get("world", [""])[0]
            if world and parts[0] in ("wake", "restart"):
                if not WORLD_ID_RE.match(world):
                    return self._send(400, {"error": "invalid world id (a-z0-9-, 3-24)"})
                extra += ["--world", world]
            return self._run(parts[0] + "/" + game, flag, game, *extra)
        # Reservierung an/aus: /reservieren/<game> | /freigeben/<game>. Ein reserviertes Spiel
        # geht nicht von selbst aus und wird nicht verdraengt, im Windows-Lab heisst dasselbe
        # 'Wartungsmodus'. rc=3 = das Spiel wird gerade gar nicht verwaltet (laeuft nicht).
        if len(parts) == 2 and parts[0] in ("reservieren", "freigeben"):
            game = parts[1]
            if game not in known_games():
                return self._send(400, {"error": "unknown game", "known": known_games()})
            flag = "--reservieren" if parts[0] == "reservieren" else "--freigeben"
            return self._run(parts[0] + "/" + game, flag, game)
        return self._send(404, {"error": "use /wake/<game>, /sleep/<game>, /restart/<game>, "
                                         "/reservieren/<game>, /freigeben/<game>, "
                                         "/worlds/<game>/create, /mc/start, /mc/stop"})

def main():
    if not load_token():
        print("WARN: kein %s -> alle wake/sleep-Requests werden 401 abgelehnt (Token anlegen!)" % TOKEN_FILE, file=sys.stderr)
    srv = ThreadingHTTPServer((BIND, PORT), H)
    print("wake-bridge: http://%s:%d  (games: %s)" % (BIND, PORT, known_games()), flush=True)
    srv.serve_forever()

if __name__ == "__main__":
    main()
