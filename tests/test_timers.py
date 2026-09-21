#!/usr/bin/env python3
"""Unit-Tests fuer die Tick-Logik des Orchestrators — ohne Node, ohne pct/qm, ohne Spielserver.

Die vorhandene arbiter-tests.sh faehrt echte Start/Stop-Zyklen auf .18 und braucht dafuer einen
freien Node und Minuten an Laufzeit. Diese Suite ersetzt sie nicht, sondern deckt ab, was dort
kaum pruefbar ist: die Zeit-Logik. Alle Sensoren werden ersetzt, die Uhr wird vorgestellt, statt
gewartet — deshalb laeuft die Suite in Sekunden und ohne Nebenwirkungen.

Aufruf:  python3 tests/test_timers.py
"""
import importlib.util, json, os, sys, tempfile, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
# Laeuft sowohl im Repo (tests/ neben arbiter/) als auch auf dem Node (/opt/game-arbiter/tests/).
ARBITER_PY = next(
    (p for p in (os.path.join(HERE, "..", "arbiter", "arbiter.py"),
                 os.path.join(HERE, "..", "arbiter.py"),
                 "/opt/game-arbiter/arbiter.py") if os.path.isfile(p)),
    os.path.join(HERE, "..", "arbiter", "arbiter.py"))


def load_arbiter(tmpdir):
    """Frische Modul-Instanz je Test, mit BASE/State/Log in einem Wegwerf-Verzeichnis."""
    sys.argv = ["arbiter.py"]
    spec = importlib.util.spec_from_file_location("arb_under_test", ARBITER_PY)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m.BASE = tmpdir
    m.STATE_FILE = os.path.join(tmpdir, "state.json")
    m.AUDIT_LOG = os.path.join(tmpdir, "audit.log")
    m.STATUS_FILE = os.path.join(tmpdir, "status.json")
    m.LOCK_FILE = os.path.join(tmpdir, "arbiter.lock")
    # ★ METRICS_FILE MUSS mit umgebogen werden. Sonst schreiben die Tick-Tests ihre
    # erfundenen Werte (TickHarness: avail=9000) in die ECHTE Datei des node-exporters,
    # und ein Testlauf auf dem Wirt verfaelscht die Ueberwachung: gemessen am 2026-08-28
    # sprang arbiter_speicher_frei_mb von 5786 auf glatte 9000. Schlimmer als der falsche
    # Messwert ist die Folge — ein solcher Ausreisser setzt spiel_startbar kurz auf 1 und
    # bricht damit die 6-Stunden-Kette von SpielDauerhaftNichtStartbar ab. Der Alarm wuerde
    # dann nie feuern, gerade weil jemand die Tests laufen laesst.
    m.METRICS_FILE = os.path.join(tmpdir, "spiele.prom")
    m.DRY = False
    # PROFILE wird beim Import aus /opt/game-arbiter/arbiter.json gelesen — auf einem Host ohne
    # Minecraft/Lab (Spiele-VPS) haetten die MC-/Lab-Tests sonst gegen ausgeschaltete Rollen
    # geprueft und waeren dort rot gewesen, obwohl der Code stimmt. Die Tests bestimmen ihr
    # Profil deshalb selbst; wer die Rollen-Weiche testen will, setzt m.PROFILE um.
    m.PROFILE = {"node": "test", "roles": {"minecraft": True, "lab": True}}
    return m


class TickHarness:
    """Ersetzt jeden Sensor und jede Aktion durch steuerbare Attrappen und protokolliert, was
    der Controller tun WOLLTE. So laesst sich jede Lage in Millisekunden herstellen."""

    def __init__(self, m, game=None, games=None):
        self.m = m
        self.calls = []
        self.mc_state = "exited"
        self.mc_health = "n/a"
        self.mc_players = 0
        self.game_running = False
        self.game_players = 0
        self.avail = 9000
        self.game = game or {
            "name": "testgame", "kind": "lxc-systemd", "ctid": 299, "service": "test",
            "probe": {"type": "a2s", "ip": "127.0.0.1", "port": 1}, "idle_timeout_s": 1200,
        }
        m.GAMES = list(games) if games else [self.game]
        # Seit dem Mehr-Spiel-Umbau kann jedes Spiel einen eigenen Zustand haben. Wer nur
        # eines prueft, benutzt weiter game_running/game_players; wer mehrere braucht,
        # traegt sie hier je Name ein.
        self.je_spiel = {}

        m.ensure_gate = lambda: True
        m.gate_running = lambda: True
        m.lab_running = lambda: []
        m.lab_session_active = lambda: False
        m.mc_lxc_running = lambda: True
        m.mc_ct_state = lambda: self.mc_state
        m.mc_health = lambda: self.mc_health
        m.mc_players = lambda: self.mc_players
        m.mc_exit_code = lambda: 0
        m.free_mb = lambda: self.avail
        m.publish_mqtt = lambda snap: None
        m.game_active = lambda g: self._zustand(g, "running")
        m.game_players = lambda g: self._zustand(g, "players")
        m.game_host_ready = lambda g: self._zustand(g, "running")
        m.game_reachable = lambda g: self._zustand(g, "players") >= 0
        # Unit-Lage je Spiel. Ohne diese Attrappe liefe dienst_lage in ein echtes
        # 'pct exec'/'systemctl show' auf dem Testrechner -- teuer und vom Zufall abhaengig.
        # Vorgabe ist der Normalfall: sauber gestoppt, nichts kaputt.
        m.dienst_lage = lambda g: self._lage(g)

        def mc_start():
            self.calls.append("mc_start"); self.mc_state = "running"; self.mc_health = "healthy"; return True

        def mc_stop():
            self.calls.append("mc_stop"); self.mc_state = "exited"; self.mc_health = "n/a"; return True

        def game_start(g, world=None):
            self.calls.append("game_start:" + g["name"])
            self._setze(g["name"], "running", True); return True

        def game_stop(g):
            self.calls.append("game_stop:" + g["name"])
            self._setze(g["name"], "running", False); return True

        m.mc_start, m.mc_stop, m.game_start, m.game_stop = mc_start, mc_stop, game_start, game_stop

    def _zustand(self, g, feld):
        eig = self.je_spiel.get(g["name"])
        if eig is not None and feld in eig:
            return eig[feld]
        return self.game_running if feld == "running" else self.game_players

    def _lage(self, g):
        """Was 'systemctl show' ueber die Unit sagt. 'gescheitert' je Spiel setzbar."""
        laeuft = self._zustand(g, "running")
        eig = self.je_spiel.get(g["name"]) or {}
        zustand = eig.get("unit") or ("active" if laeuft else "inactive")
        return {"aktiv": zustand == "active", "gescheitert": zustand == "failed",
                "zustand": zustand, "unterzustand": "", "ergebnis": eig.get("ergebnis", "success"),
                "speicher_bytes": eig.get("speicher_bytes"), "cpu_ns": None, "neustarts": 0}

    def _setze(self, name, feld, wert):
        if name in self.je_spiel:
            self.je_spiel[name][feld] = wert
        elif feld == "running":
            self.game_running = wert
        else:
            self.game_players = wert

    def reserviere(self, st, *namen):
        """Spiel(e) reservieren wie es --wake tut, ohne Start/Stopp auszuloesen."""
        for n in namen:
            self.m.reserve_game(st, n)
            st["games"][n]["since"] = None      # kein Start-Fenster in den Zeit-Tests
        return st

    def slot(self, st, name):
        return st["games"].get(name, {})

    def tick(self, st):
        return self.m.tick(st, False, False)

    def advance(self, st, seconds):
        """Uhr vorstellen: alle Startzeitpunkte im State um `seconds` in die Vergangenheit ruecken."""
        for k in ("mc_unused_since", "mc_idle_since", "lab_idle_since", "lab_since"):
            if isinstance(st.get(k), (int, float)):
                st[k] -= seconds
        for slot in (st.get("games") or {}).values():
            for k in ("unused_since", "idle_since", "since", "start_versuch"):
                if isinstance(slot.get(k), (int, float)):
                    slot[k] -= seconds
            # naechster_versuch liegt in der ZUKUNFT -> die Uhr vorstellen heisst hier,
            # den Zeitpunkt naeher heranzuholen, nicht ihn weiter wegzuschieben.
            if isinstance(slot.get("naechster_versuch"), (int, float)) and slot["naechster_versuch"]:
                slot["naechster_versuch"] -= seconds
        for slot in (st.get("log_last") or {}).values():
            if isinstance(slot.get("ts"), (int, float)):
                slot["ts"] -= seconds
        return st


class GameUnusedTimeout(unittest.TestCase):
    """Der Kernbefund: ein geweckter, aber nie betretener Server lief unbegrenzt weiter.
    Im Audit-Log der ersten drei Wochen 63,6 h Leerlauf, die laengste Phase 38 h am Stueck."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.m = load_arbiter(self.tmp)
        self.h = TickHarness(self.m)
        self.st = self.m.load_state()
        self.h.reserviere(self.st, "testgame")
        self.h.game_running = True

    def test_nie_betreten_wird_abgeschaltet(self):
        self.h.game_players = 0
        self.h.tick(self.st)
        self.assertIsNotNone(self.h.slot(self.st, "testgame").get("unused_since"), "Grace-Uhr muss anlaufen, sobald der Server antwortet")
        self.assertNotIn("game_stop:testgame", self.h.calls, "vor Ablauf darf nichts gestoppt werden")

        self.h.advance(self.st, 899)
        self.h.tick(self.st)
        self.assertNotIn("game_stop:testgame", self.h.calls, "eine Sekunde vor Ablauf laeuft er noch")

        self.h.advance(self.st, 2)
        self.h.tick(self.st)
        self.assertIn("game_stop:testgame", self.h.calls, "nach 900 s ohne einen einzigen Spieler -> aus")
        self.assertNotIn("testgame", self.st["games"], "Reservierung muss zurueckfallen, sonst weckt der naechste Tick erneut")

    def test_spieler_kommt_stoppt_die_uhr(self):
        self.h.game_players = 0
        self.h.tick(self.st)
        self.h.advance(self.st, 800)
        self.h.game_players = 2                      # jemand joint kurz vor Ablauf
        self.h.tick(self.st)
        self.assertIsNone(self.h.slot(self.st, "testgame").get("unused_since"), "Grace-Uhr muss beim ersten Spieler verfallen")
        self.assertTrue(self.h.slot(self.st, "testgame").get("was_used"))
        self.h.advance(self.st, 5000)                # lange spielen
        self.h.tick(self.st)
        self.assertNotIn("game_stop:testgame", self.h.calls, "ein bespielter Server darf nie am Grace-Timer sterben")

    def test_bootphase_zaehlt_nicht_mit(self):
        """Solange die Probe keinen Kontakt hat (-1), ist der Server nicht joinbar — in dieser Zeit
        darf die Uhr nicht laufen, sonst stirbt ein langsam startender Server (DayZ braucht Minuten)."""
        self.h.game_players = -1
        for _ in range(3):
            self.h.tick(self.st)
        self.assertIsNone(self.h.slot(self.st, "testgame").get("unused_since"), "kein Kontakt = bootet, das ist kein Leerlauf")
        self.assertNotIn("game_stop:testgame", self.h.calls)

    def test_nach_nutzung_gilt_der_idle_timer(self):
        """Wer gespielt hat und geht, faellt in idle_timeout_s (1200) — nicht in die kuerzere Grace-Uhr."""
        self.h.game_players = 1
        self.h.tick(self.st)
        self.h.game_players = 0
        self.h.tick(self.st)
        self.assertIsNotNone(self.h.slot(self.st, "testgame").get("idle_since"))
        self.h.advance(self.st, 901)
        self.h.tick(self.st)
        self.assertNotIn("game_stop:testgame", self.h.calls, "900 s sind die Grace-Grenze, nach Nutzung gelten 1200 s")
        self.h.advance(self.st, 300)
        self.h.tick(self.st)
        self.assertIn("game_stop:testgame", self.h.calls)

    def test_immer_online_bleibt_unangetastet(self):
        """idle_timeout_s=0 ist eine bewusste Owner-Entscheidung (Einrichtungsphase) und muss
        auch die neue Uhr abschalten."""
        self.h.game["idle_timeout_s"] = 0
        self.h.game_players = 0
        self.h.tick(self.st)
        self.h.advance(self.st, 99999)
        self.h.tick(self.st)
        self.assertNotIn("game_stop:testgame", self.h.calls)
        self.assertIsNone(self.h.slot(self.st, "testgame").get("unused_since"))

    def test_je_game_abschaltbar(self):
        self.h.game["unused_timeout_s"] = 0
        self.h.game_players = 0
        self.h.tick(self.st)
        self.h.advance(self.st, 99999)
        self.h.tick(self.st)
        self.assertNotIn("game_stop:testgame", self.h.calls, "unused_timeout_s=0 muss die Uhr je Game abschalten")

    def test_probe_ohne_spielerzahl_loest_nie_aus(self):
        """Eine reine tcp-Probe sagt nur 'Port offen'. Daraus 'niemand da' abzuleiten waere falsch
        und wuerde einen bespielten Server abschiessen."""
        self.h.game["probe"] = {"type": "tcp", "ip": "127.0.0.1", "port": 1}
        self.h.game_players = 0
        self.h.tick(self.st)
        self.h.advance(self.st, 99999)
        self.h.tick(self.st)
        self.assertNotIn("game_stop:testgame", self.h.calls)
        self.assertIsNone(self.h.slot(self.st, "testgame").get("unused_since"))


class McUnusedTimeout(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.m = load_arbiter(self.tmp)
        self.h = TickHarness(self.m)
        self.st = self.m.load_state()
        self.st["reservation"] = "minecraft"
        self.h.mc_state = "running"; self.h.mc_health = "healthy"

    def test_nie_betreten_wird_abgeschaltet(self):
        self.h.mc_players = 0
        self.h.tick(self.st)
        self.assertIsNotNone(self.st["mc_unused_since"])
        self.h.advance(self.st, 901)
        self.h.tick(self.st)
        self.assertIn("mc_stop", self.h.calls)
        self.assertEqual(self.st["reservation"], "none")

    def test_spieler_haelt_es_wach(self):
        self.h.mc_players = 1
        self.h.tick(self.st)
        self.h.advance(self.st, 99999)
        self.h.tick(self.st)
        self.assertNotIn("mc_stop", self.h.calls)

    def test_startphase_zaehlt_nicht(self):
        """health='starting' heisst: noch nicht joinbar. Die Uhr darf erst bei healthy laufen."""
        self.h.mc_health = "starting"; self.h.mc_players = -1
        for _ in range(3):
            self.h.tick(self.st)
        self.assertIsNone(self.st["mc_unused_since"])
        self.assertNotIn("mc_stop", self.h.calls)


class AuditLogEntrauschung(unittest.TestCase):
    """73 % des Logs waren identische Wiederholungen im Minutentakt — echte Ereignisse gingen
    darin unter."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.m = load_arbiter(self.tmp)
        self.h = TickHarness(self.m)
        self.st = self.m.load_state()

    def lines(self):
        try:
            with open(self.m.AUDIT_LOG) as f:
                return [l for l in f.read().splitlines() if l.strip()]
        except OSError:
            return []

    def test_unveraenderte_lage_schreibt_einmal(self):
        for _ in range(60):                      # eine Stunde Ticks bei stabiler Lage
            self.h.tick(self.st)
        n = len(self.lines())
        self.assertLessEqual(n, 4, "eine Stunde unveraenderter Leerlauf darf das Log kaum fuellen, war: %d" % n)
        self.assertGreaterEqual(n, 1, "die erste Zeile muss geschrieben werden")

    def test_heartbeat_kommt_wieder(self):
        """Stille darf nicht heissen 'Controller tot' — nach LOG_HEARTBEAT_S kommt ein Lebenszeichen."""
        self.h.tick(self.st)
        before = len(self.lines())
        self.h.advance(self.st, self.m.LOG_HEARTBEAT_S + 60)
        self.h.tick(self.st)
        self.assertEqual(len(self.lines()), before + 1)
        self.assertIn("unveraendert seit", self.lines()[-1])

    def test_zustandswechsel_wird_immer_geschrieben(self):
        self.h.tick(self.st)
        self.h.tick(self.st)
        before = len(self.lines())
        self.st["reservation"] = "minecraft"      # Lagewechsel -> MC soll starten
        self.h.tick(self.st)
        self.assertGreater(len(self.lines()), before, "ein Zustandswechsel darf nie unterdrueckt werden")
        self.assertTrue(any("TRANSITION" in l for l in self.lines()))

    def test_aktionen_werden_nie_unterdrueckt(self):
        self.h.reserviere(self.st, "testgame"); self.h.game_running = True; self.h.game_players = 0
        self.h.tick(self.st)
        self.h.advance(self.st, 901)
        self.h.tick(self.st)
        self.assertTrue(any("nie betreten" in l for l in self.lines()),
                        "die Abschalt-Begruendung muss im Log stehen")

    def test_rotation_greift(self):
        self.m.LOG_MAX_BYTES = 2048
        for i in range(400):
            self.m.audit("Zeile %d mit etwas Text, damit die Datei waechst" % i)
        self.assertTrue(os.path.exists(self.m.AUDIT_LOG + ".1"), "audit.log muss rotieren")
        self.assertLess(os.path.getsize(self.m.AUDIT_LOG), 2048 + 4096)

    def test_volle_platte_haelt_den_controller_nicht_an(self):
        self.m.AUDIT_LOG = "/proc/gibt-es-nicht/audit.log"
        self.h.tick(self.st)              # darf nicht werfen
        self.assertEqual(self.st["mode"], "IDLE")


class LxcShutdownVerifikation(unittest.TestCase):
    """'pct shutdown' meldet Erfolg, sobald der Auftrag abgesetzt ist. Am 20.08. lief LXC 206
    danach noch stundenlang weiter, waehrend das Log 'Node idle' behauptete."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.m = load_arbiter(self.tmp)
        self.m.DRY = False
        self.m.time.sleep = lambda s: None
        self.cmds = []
        self.status = ["running"]          # Antworten von 'pct status', der Reihe nach

    def fake_run(self, cmd, timeout=30):
        self.cmds.append(cmd)
        if cmd.startswith("pct status"):
            return 0, "status: " + (self.status.pop(0) if len(self.status) > 1 else self.status[0]), ""
        return 0, "", ""

    def test_erfolgreicher_stopp(self):
        self.status = ["stopped"]
        self.m.run = self.fake_run
        self.assertTrue(self.m._shutdown_lxc(206, "valheim"))
        self.assertEqual(sum(1 for c in self.cmds if c.startswith("pct shutdown")), 1,
                         "wer wirklich unten ist, wird nicht noch einmal gestoppt")

    def test_haengender_container_wird_erkannt(self):
        self.status = ["running", "running", "running"]
        self.m.run = self.fake_run
        self.assertFalse(self.m._shutdown_lxc(206, "valheim"), "haengender LXC darf nicht als Erfolg gelten")
        self.assertEqual(sum(1 for c in self.cmds if c.startswith("pct shutdown")), 2, "genau einmal nachfassen")
        log = open(self.m.AUDIT_LOG).read()
        self.assertIn("bleibt oben", log, "das Log muss den Rest-Container benennen")
        self.assertNotIn("pct stop 206\n", " ".join(self.cmds), "kein Hard-Kill ohne Bestaetigung")

    def test_zweiter_versuch_reicht(self):
        self.status = ["running", "stopped"]
        self.m.run = self.fake_run
        self.assertTrue(self.m._shutdown_lxc(206, "valheim"))


class StatusSnapshot(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.m = load_arbiter(self.tmp)
        self.h = TickHarness(self.m)
        self.st = self.m.load_state()

    def test_restlaufzeit_steht_im_snapshot(self):
        """Dashboard und Discord sollen 'geht in X min aus' anzeigen koennen."""
        self.h.reserviere(self.st, "testgame"); self.h.game_running = True; self.h.game_players = 0
        self.h.tick(self.st)
        with open(self.m.STATUS_FILE) as f:
            snap = json.load(f)
        self.assertIsNotNone(snap["games"]["unused_since"])
        self.assertIn("unused_since", snap["mc"])
        self.assertEqual(snap["games"]["active_players"], 0)
        # Mehr-Spiel-Sicht daneben: reserved bleibt ein Name (alte Leser), reserved_all ist die Wahrheit
        self.assertEqual(snap["games"]["reserved"], "testgame")
        self.assertEqual(snap["games"]["reserved_all"], ["testgame"])
        self.assertEqual(snap["games"]["detail"]["testgame"]["players"], 0)

    def test_erreichbar_trennt_bootend_von_leer(self):
        """players=null hiess bisher zweierlei: 'antwortet nicht' (bootet noch) und 'kein
        Kontakt'. Die Oberflaeche zeigte beides als 'laeuft' — wer daraufhin beitrat, lief
        in einen Timeout. erreichbar macht den Unterschied sichtbar."""
        self.h.reserviere(self.st, "testgame"); self.h.game_running = True

        self.h.game_players = 0            # antwortet, gerade leer
        self.h.tick(self.st)
        with open(self.m.STATUS_FILE) as f: snap = json.load(f)
        d = snap["games"]["detail"]["testgame"]
        self.assertEqual(d["players"], 0)
        self.assertIs(d["erreichbar"], True, "0 Spieler heisst: er antwortet")

        self.h.game_players = -1           # gefragt, keine Antwort -> bootet noch
        self.h.tick(self.st)
        with open(self.m.STATUS_FILE) as f: snap = json.load(f)
        d = snap["games"]["detail"]["testgame"]
        self.assertIsNone(d["players"], "keine Antwort ist keine Spielerzahl")
        self.assertIs(d["erreichbar"], False, "und genau das muss unterscheidbar bleiben")

    def test_erreichbar_bei_tcp_conn_fragt_den_port(self):
        """★ Der Fall, an dem die erste Fassung scheiterte: `tcp-conn` zaehlt bestehende
        Verbindungen. Null davon hat ein bootender Server genauso wie ein laufender, auf
        dem niemand spielt — gemessen an Terraria sah ein frisch gestarteter Server schon
        in der ersten Sekunde aus wie 'laeuft'. Bei dieser Probenart muss zusaetzlich der
        Port gefragt werden, sonst ist die ganze Unterscheidung bei genau dem Spiel
        wirkungslos, das mehrere Welten hat."""
        self.m.GAMES = [{"name": "tg", "kind": "systemd", "service": "tg", "idle_timeout_s": 1200,
                         "min_free_mb": 1000, "probe": {"type": "tcp-conn", "ip": "127.0.0.1", "port": 7777}}]
        gefragt = []
        self.m.game_reachable = lambda g: (gefragt.append(g["name"]), False)[1]
        self.assertIs(self.m._erreichbar("tg", 0), False, "0 Verbindungen + toter Port = bootet noch")
        self.assertEqual(gefragt, ["tg"], "bei tcp-conn muss der Port wirklich gefragt werden")

        self.m.game_reachable = lambda g: True
        self.assertIs(self.m._erreichbar("tg", 0), True, "0 Verbindungen + lauschender Port = leer, aber da")

        # Gegenprobe a2s: dort ist eine Zahl >= 0 schon der Beweis, keine zweite Probe noetig.
        self.m.GAMES = [{"name": "ta", "kind": "systemd", "service": "ta", "idle_timeout_s": 1200,
                         "min_free_mb": 1000, "probe": {"type": "a2s", "ip": "127.0.0.1", "port": 1}}]
        gefragt.clear()
        self.m.game_reachable = lambda g: (gefragt.append(g["name"]), True)[1]
        self.assertIs(self.m._erreichbar("ta", 0), True)
        self.assertIs(self.m._erreichbar("ta", -1), False, "-1 heisst: gefragt, keine Antwort")
        self.assertEqual(gefragt, [], "a2s braucht keine zweite Probe")
        self.assertIsNone(self.m._erreichbar("ta", None), "nicht gefragt heisst nicht 'aus'")

    def test_bedarf_und_startreserve_stehen_im_snapshot(self):
        """Damit eine Oberflaeche VOR dem Klick dieselbe Rechnung machen kann wie
        precheck_game — statt die Zahlen zu kopieren und beim naechsten Nachmessen
        still falsch zu liegen."""
        self.h.tick(self.st)
        with open(self.m.STATUS_FILE) as f: snap = json.load(f)
        bedarf = snap["games"]["bedarf"]
        self.assertIn("testgame", bedarf)
        self.assertIn("min_free_mb", bedarf["testgame"])
        self.assertIn("ram_mb", bedarf["testgame"])
        self.assertIn("reserviert_startend_mb", snap["ram"])
        # Jedes Spiel der Registry ist vertreten — sonst faellt genau die Kachel ohne
        # Vorschau aus, die neu dazugekommen ist.
        self.assertEqual(set(bedarf), set(self.m.game_names()))


def zwei_spiele():
    """Zwei Registry-Eintraege: ein leichtes und ein schweres, beide mit echter Spielerzahl."""
    return [
        {"name": "leicht", "kind": "systemd", "service": "leicht", "idle_timeout_s": 1200,
         "min_free_mb": 1000, "ram_mb": 1200, "probe": {"type": "a2s", "ip": "127.0.0.1", "port": 1}},
        {"name": "schwer", "kind": "systemd", "service": "schwer", "idle_timeout_s": 1200,
         "min_free_mb": 4000, "ram_mb": 4000, "probe": {"type": "a2s", "ip": "127.0.0.1", "port": 2}},
    ]


class MehrereSpieleGleichzeitig(unittest.TestCase):
    """Der Arbiter durfte bis 2026-08-22 genau ein Spiel laufen lassen (reservation als String).
    Auf dem Spiele-VPS laufen vier nebeneinander — ein unveraenderter Tick haette drei davon
    abgeraeumt, darunter ein besetztes Terraria."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.m = load_arbiter(self.tmp)
        self.h = TickHarness(self.m, games=zwei_spiele())
        self.st = self.m.load_state()
        self.h.je_spiel = {"leicht": {"running": False, "players": 0},
                           "schwer": {"running": False, "players": 0}}

    def test_besetztes_spiel_wird_nie_abgeraeumt(self):
        """DER Sicherheitsgurt: laeuft ein Spiel mit Spielern ohne Reservierung — etwa weil es
        von Hand oder beim Systemstart hochkam — wird es uebernommen, nicht gestoppt."""
        self.h.je_spiel["leicht"] = {"running": True, "players": 2}
        self.h.tick(self.st)
        self.assertNotIn("game_stop:leicht", self.h.calls, "ein besetztes Spiel darf der Tick nie stoppen")
        self.assertIn("leicht", self.st["games"], "es muss stattdessen uebernommen werden")
        self.assertTrue(self.h.slot(self.st, "leicht")["was_used"], "wer Spieler hat, gilt als benutzt")

    def test_leeres_unreserviertes_spiel_wird_abgeraeumt(self):
        self.h.je_spiel["leicht"] = {"running": True, "players": 0}
        self.h.tick(self.st)
        self.assertIn("game_stop:leicht", self.h.calls)

    def test_zwei_spiele_haben_eigene_uhren(self):
        self.h.je_spiel = {"leicht": {"running": True, "players": 1},
                           "schwer": {"running": True, "players": 1}}
        self.h.reserviere(self.st, "leicht", "schwer")
        self.h.tick(self.st)
        self.h.je_spiel["leicht"]["players"] = 0          # nur eines wird leer
        self.h.tick(self.st)
        self.h.advance(self.st, 1300)
        self.h.tick(self.st)
        self.assertIn("game_stop:leicht", self.h.calls, "das leere Spiel geht aus")
        self.assertNotIn("game_stop:schwer", self.h.calls, "das bespielte laeuft weiter")
        self.assertIn("schwer", self.st["games"])

    def test_start_verdraengt_niemanden_wenn_platz_ist(self):
        self.h.je_spiel["leicht"] = {"running": True, "players": 2}
        self.h.reserviere(self.st, "leicht")
        self.h.avail = 9000                                # reichlich frei
        r = self.m.cmd_start_game(self.st, "schwer")
        self.assertEqual(r, "started")
        self.assertNotIn("game_stop:leicht", self.h.calls, "bei genug RAM weicht niemand")
        self.assertEqual(sorted(self.st["games"]), ["leicht", "schwer"])

    def test_bei_ram_knappheit_weicht_das_leere_nicht_das_besetzte(self):
        self.m.time.sleep = lambda s: None
        self.h.je_spiel = {"leicht": {"running": True, "players": 0}}
        self.h.reserviere(self.st, "leicht")
        self.h.avail = 3500                                # zu wenig fuer 'schwer' (4000)
        r = self.m.cmd_start_game(self.st, "schwer")
        self.assertIn("game_stop:leicht", self.h.calls, "das leere Spiel macht Platz")
        self.assertEqual(r, "started")

    def test_besetztes_spiel_blockiert_den_start_statt_zu_weichen(self):
        self.m.time.sleep = lambda s: None
        self.h.je_spiel = {"leicht": {"running": True, "players": 3}}
        self.h.reserviere(self.st, "leicht")
        self.h.avail = 3500
        r = self.m.cmd_start_game(self.st, "schwer")
        self.assertEqual(r, "rejected", "lieber kein Start als jemanden aus dem Spiel werfen")
        self.assertNotIn("game_stop:leicht", self.h.calls)

    def test_erzwingen_gibt_es_nicht_mehr(self):
        """Der Erzwingen-Weg wurde am 2026-08-23 ersatzlos entfernt (Owner-Ansage: wer
        spielt, wird nicht gekickt). Dieser Test hiess vorher 'test_force_verdraengt_auch
        _besetztes' und pruefte das Gegenteil — er blieb nach dem Ausbau stehen und war
        seitdem rot, also lief die Suite ein Jahresviertel lang nicht gruen durch.

        Ein Test, der einer abgeschafften Faehigkeit nachtrauert, ist schlimmer als kein
        Test: das erwartete Rot deckt jedes echte Rot daneben zu. Er prueft jetzt, was
        gelten SOLL — dass die Faehigkeit weg ist und auch nicht heimlich zurueckkommt.
        """
        import inspect
        self.assertNotIn("force", inspect.signature(self.m.cmd_start_game).parameters,
                         "cmd_start_game darf keinen force-Parameter mehr annehmen")
        self.m.time.sleep = lambda s: None
        self.h.je_spiel = {"leicht": {"running": True, "players": 3}}
        self.h.reserviere(self.st, "leicht")
        self.h.avail = 3500
        r = self.m.cmd_start_game(self.st, "schwer")
        self.assertEqual(r, "rejected", "ein besetztes Spiel bleibt besetzt")
        self.assertNotIn("game_stop:leicht", self.h.calls, "niemand wird aus dem Spiel geworfen")
        self.assertIn("leicht", self.st["games"], "und die Reservierung bleibt bestehen")

    def test_laufendes_spiel_wird_nicht_neu_gestartet(self):
        """Der Bot ruft /wake auch, wenn jemand nur nachsehen will. Ein Neustart waere das
        Gegenteil dessen, was gemeint ist — und wuerfe die Anwesenden heraus."""
        self.h.je_spiel["leicht"] = {"running": True, "players": 2}
        r = self.m.cmd_start_game(self.st, "leicht")
        self.assertEqual(r, "already-running")
        self.assertNotIn("game_start:leicht", self.h.calls)
        self.assertIn("leicht", self.st["games"], "die Reservierung wird dabei bestaetigt")

    def test_startfenster_verhindert_den_doppelten_schweren_start(self):
        """Ein frisch gestartetes Spiel hat sein RAM noch nicht belegt. Ohne diese Buchhaltung
        kaemen zwei schwere Starts kurz hintereinander beide durch — und der zweite killt den
        ersten per OOM."""
        self.h.avail = 5000
        self.m.cmd_start_game(self.st, "schwer")
        self.h.je_spiel["schwer"] = {"running": True, "players": -1}   # bootet noch, antwortet nicht
        self.st["games"]["schwer"]["since"] = time.time()
        rest = self.m.booting_reserve_mb(self.st)
        self.assertEqual(rest, 4000, "der angemeldete Bedarf muss mitzaehlen, solange er nicht sichtbar ist")
        self.assertFalse(self.m.precheck_game(self.m.game_by_name("schwer"), 5000, self.st) is True and rest == 0)

    def test_nach_neustart_startet_der_tick_nicht_alles_auf_einmal(self):
        """Nach einem Neustart des Wirts sind alle Reservierungen noch da und die Dienste aus.
        Der Tick startet sie der Reihe nach — und muss dabei mitzaehlen, was die eben
        gestarteten gleich belegen werden. Sonst passen im selben Durchgang mehr Spiele
        hinein, als der Wirt tragen kann, und der OOM-Killer entscheidet."""
        self.h.je_spiel = {"leicht": {"running": False, "players": -1},
                           "schwer": {"running": False, "players": -1}}
        self.h.reserviere(self.st, "leicht", "schwer")
        self.h.avail = 4500          # reicht fuer EINES der beiden (schwer braucht 4000)
        self.h.tick(self.st)
        gestartet = [c for c in self.h.calls if c.startswith("game_start")]
        self.assertIn("game_start:leicht", gestartet)
        self.assertNotIn("game_start:schwer", gestartet,
                         "das zweite Spiel muss warten, bis der Speicher des ersten sichtbar ist")
        self.assertIsNotNone(self.h.slot(self.st, "leicht")["since"],
                             "der Tick muss ein Start-Fenster setzen, sonst zaehlt der Bedarf nicht mit")

    def test_immer_online_wird_auch_nicht_verdraengt(self):
        """idle_timeout_s=0 ist eine Owner-Ansage ('laeuft durch'). Der Auto-Off-Zweig hat das
        immer geachtet, der Verdraengungspfad nicht — am 2026-08-22 opferte er prompt den
        DayZ-Server, weil gerade niemand darauf spielte. Wer ihn wirklich weghaben will,
        nimmt --force."""
        self.m.time.sleep = lambda s: None
        self.m.GAMES[0]["idle_timeout_s"] = 0          # 'leicht' ist jetzt Dauerlaeufer
        self.h.je_spiel = {"leicht": {"running": True, "players": 0}}
        self.h.reserviere(self.st, "leicht")
        self.h.avail = 3500                            # zu wenig fuer 'schwer'
        r = self.m.cmd_start_game(self.st, "schwer")
        self.assertNotIn("game_stop:leicht", self.h.calls, "ein Dauerlaeufer darf nicht geopfert werden")
        self.assertEqual(r, "no-ram", "dann lieber kein Start")
        self.assertIn("leicht", self.st["games"])

    def test_adopt_uebernimmt_ohne_start_oder_stopp(self):
        """Der Schritt, mit dem ein Arbiter einen Host uebernimmt, auf dem die Spiele schon
        laufen. Er darf dabei nichts anfassen."""
        self.h.je_spiel = {"leicht": {"running": True, "players": 2},
                           "schwer": {"running": False, "players": 0}}
        self.m.cmd_adopt(self.st)
        self.assertEqual(list(self.st["games"]), ["leicht"], "nur Laufendes wird uebernommen")
        self.assertTrue(self.h.slot(self.st, "leicht")["was_used"])
        self.assertEqual(self.h.calls, [], "kein einziger Start, kein einziger Stopp")


class HostNativeSpiele(unittest.TestCase):
    """kind 'systemd' = das Spiel laeuft neben dem Arbiter statt in einem Proxmox-LXC.
    Auf dem Spiele-VPS gibt es kein pct — ein uebersehener Aufruf liefe dort ins Leere."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.m = load_arbiter(self.tmp)
        self.cmds = []
        self.m.DRY = False
        self.m.run = lambda cmd, timeout=30: (self.cmds.append(cmd), (0, "active", ""))[1]

    def test_host_nativ_ruft_kein_pct(self):
        g = {"name": "valheim", "kind": "systemd", "service": "valheim-server"}
        self.m.game_start(g); self.m.game_stop(g); self.m.game_active(g)
        self.assertTrue(self.cmds, "es muss ueberhaupt etwas ausgefuehrt worden sein")
        self.assertFalse(any("pct" in c for c in self.cmds), "host-nativ darf nie pct aufrufen: %s" % self.cmds)
        self.assertIn("systemctl start valheim-server", self.cmds)

    def test_lxc_spiel_ruft_weiter_pct(self):
        g = {"name": "zomboid", "kind": "lxc-systemd", "ctid": 208, "service": "zomboid-server"}
        self.m._lxc_running = lambda ctid: True
        self.m.game_start(g)
        self.assertTrue(any(c.startswith("pct exec 208 -- systemctl start") for c in self.cmds),
                        "LXC-Spiele muessen unveraendert ueber pct laufen: %s" % self.cmds)

    def test_host_nativ_faehrt_keinen_container_herunter(self):
        """Nach dem Stopp eines LXC-Spiels faehrt der Arbiter den Container herunter. Host-nativ
        gibt es keinen — ein 'pct shutdown' waere dort bestenfalls wirkungslos."""
        g = {"name": "valheim", "kind": "systemd", "service": "valheim-server"}
        self.m.game_stop(g)
        self.assertFalse(any("shutdown" in c for c in self.cmds))


class RollenWeiche(unittest.TestCase):
    """Das Host-Profil sagt, welche Rollen es an diesem Ort gibt. Auf dem Spiele-VPS existieren
    weder Minecraft (LXC 203) noch das Win-Lab (VM 210/211) — der Tick darf sie dort nicht
    einmal fragen, sonst laeuft er jede Minute in ein fehlendes pct/qm."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.m = load_arbiter(self.tmp)
        self.m.PROFILE = {"node": "gamehost", "roles": {"minecraft": False, "lab": False}}
        self.gefragt = []
        self.h = TickHarness(self.m)
        for name in ("ensure_gate", "mc_lxc_running", "mc_ct_state", "mc_players", "lab_running"):
            def spion(*a, _n=name, **k):
                self.gefragt.append(_n)
                return {"ensure_gate": True, "mc_lxc_running": True, "mc_ct_state": "running",
                        "mc_players": 0, "lab_running": [210]}[_n]
            setattr(self.m, name, spion)
        self.st = self.m.load_state()

    def test_ohne_rollen_wird_nichts_gefragt(self):
        self.h.tick(self.st)
        self.assertEqual(self.gefragt, [], "kein MC-/Lab-Sensor darf angefasst werden: %s" % self.gefragt)
        self.assertEqual(self.st["mode"], "IDLE")

    def test_node_name_steht_im_snapshot(self):
        self.h.tick(self.st)
        with open(self.m.STATUS_FILE) as f:
            snap = json.load(f)
        self.assertEqual(snap["node"], "gamehost", "der Snapshot muss sagen, WELCHER Wirt spricht")

    def test_proxyschicht_bleibt_bewacht_ohne_mc_rolle(self):
        """Wird Minecraft ein normales Registry-Spiel, muss roles.minecraft aus — die
        ALWAYS-ON-Proxyschicht (Velocity + NanoLimbo) soll trotzdem bewacht bleiben. Sie ist
        der einzige Weckweg, der ohne Discord auskommt."""
        self.m.PROFILE = {"node": "node18", "roles": {"minecraft": False, "mc_gate": True, "lab": False}}
        self.h.tick(self.st)
        self.assertIn("ensure_gate", self.gefragt, "die Proxyschicht muss weiter geprueft werden")
        self.assertNotIn("mc_ct_state", self.gefragt, "die MC-Maschine selbst aber nicht mehr")

    def test_ohne_eigene_angabe_folgt_das_gate_der_mc_rolle(self):
        """Rueckwaertskompatibel: wer mc_gate nicht setzt, bekommt das bisherige Verhalten."""
        self.m.PROFILE = {"node": "node18", "roles": {"minecraft": True, "lab": True}}
        self.assertTrue(self.m.has_role("mc_gate"))
        self.m.PROFILE = {"node": "gamehost", "roles": {"minecraft": False, "lab": False}}
        self.assertFalse(self.m.has_role("mc_gate"))

    def test_mit_rollen_werden_sie_gefragt(self):
        self.m.PROFILE = {"node": "node18", "roles": {"minecraft": True, "lab": True}}
        self.h.tick(self.st)
        self.assertIn("mc_ct_state", self.gefragt)
        self.assertIn("lab_running", self.gefragt)


class MinecraftAlsRegistrySpiel(unittest.TestCase):
    """Minecraft war jahrelang eine eingebaute Sonderrolle mit eigener Health-Maschine. Seit dem
    Umzug auf den Spiele-VPS ist es dort ein gewoehnlicher Registry-Eintrag — die CLI-Verben
    schalteten aber weiter unbedingt auf die Sonderrolle um. Folge: --wake wurde von der alten
    Exklusivitaetsregel abgelehnt ('anderes Spiel hat Vorrang'), und --probe meldete alles auf
    false, waehrend der Container gesund lief."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.m = load_arbiter(self.tmp)
        self.eintrag = {"name": "minecraft", "kind": "docker", "container": "mc-poc",
                        "probe": {"type": "rcon-cli"}, "min_free_mb": 2600, "ram_mb": 2400,
                        "idle_timeout_s": 1200}
        self.h = TickHarness(self.m, games=[self.eintrag])
        self.m.PROFILE = {"node": "gamehost", "roles": {"minecraft": False, "lab": False}}
        self.st = self.m.load_state()

    def test_registry_eintrag_schlaegt_die_sonderrolle(self):
        self.assertFalse(self.m.mc_sonderrolle("minecraft"))
        self.h.game_running = False
        self.h.game_players = 0
        self.h.avail = 9000
        r = self.m.cmd_start_game(self.st, "minecraft")
        self.assertEqual(r, "started", "der Registry-Eintrag muss ganz normal starten")
        self.assertIn("game_start:minecraft", self.h.calls)
        self.assertIn("minecraft", self.st["games"])

    def test_probe_liest_den_registry_eintrag(self):
        self.h.game_running = True
        self.h.game_players = 2
        d = self.m.cmd_probe("minecraft")
        self.assertTrue(d["service_active"], "der laufende Container muss als aktiv gelten")
        self.assertEqual(d["players"], 2)

    def test_ohne_eintrag_bleibt_die_sonderrolle(self):
        """Rueckwaertskompatibel: wo Minecraft NICHT in der Registry steht, gilt weiter die
        eingebaute Maschine mit ihrer Crash-Erkennung."""
        self.m.GAMES = []
        self.m.PROFILE = {"node": "node18", "roles": {"minecraft": True, "lab": True}}
        self.assertTrue(self.m.mc_sonderrolle("minecraft"))


class TestsFassenDenWirtNichtAn(unittest.TestCase):
    """Jeder Pfad, auf dem der Arbiter etwas schreibt, muss im Wegwerf-Verzeichnis landen.

    Anlass: die Tick-Tests schrieben ihre erfundenen Werte in die ECHTE Metrikdatei des
    node-exporters, weil load_arbiter() zwar BASE/STATE/STATUS umbog, METRICS_FILE aber
    nicht. Ein Testlauf auf dem Wirt hat damit die Ueberwachung verfaelscht — und ein
    solcher Ausreisser bricht die Karenzzeit der Alarme."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.m = load_arbiter(self.tmp)

    def test_alle_schreibpfade_liegen_im_wegwerf_verzeichnis(self):
        for name in ("BASE", "STATE_FILE", "AUDIT_LOG", "STATUS_FILE", "LOCK_FILE",
                     "METRICS_FILE"):
            pfad = getattr(self.m, name)
            self.assertTrue(pfad.startswith(self.tmp),
                            f"{name} zeigt auf {pfad} — ausserhalb des Testverzeichnisses")

    def test_kein_schreibpfad_zeigt_in_den_exporter_ordner(self):
        """Namentlich, weil genau dieser Ordner dem node-exporter gehoert und ein
        Fremdschreiber dort nicht auffaellt: die Datei sieht danach normal aus."""
        for name in ("STATE_FILE", "STATUS_FILE", "METRICS_FILE", "AUDIT_LOG"):
            self.assertNotIn("/var/lib/prometheus", getattr(self.m, name))


class AbsageIstKeinErfolg(unittest.TestCase):
    """Der Rueckgabewert von cmd_start_game wird zum Exit-Code und darueber zur Meldung,
    die ein Spieler in Discord sieht. Bis zum 2026-08-27 stand dort eine Negativ-Liste
    (`3 if r in ("rejected","bad-world") else 0`) — und weil "no-ram" spaeter dazukam, ohne
    aufgenommen zu werden, meldete eine Speicher-Absage rc=0. Der Bot zeigte einen
    Ladebalken fuer einen Server, der nie startete. Das ist heute keine Theorie: solange
    das 14B laeuft, ist DayZ dauerhaft nicht startbar."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.m = load_arbiter(self.tmp)

    def test_nur_start_und_bereits_laufend_gelten_als_erfolg(self):
        self.assertEqual(set(self.m.START_ERFOLG), {"started", "already-running"})

    def test_jede_absage_faellt_auf_die_fehlerseite(self):
        """Der Kern: die Liste ist eine POSITIV-Liste. Kommt morgen ein neuer
        Rueckgabewert dazu, ist er automatisch ein Fehler statt still ein Erfolg —
        genau der Weg, auf dem 'no-ram' durchgerutscht ist."""
        for absage in ("no-ram", "unknown", "rejected", "bad-world", "voellig-neuer-fall"):
            self.assertNotIn(absage, self.m.START_ERFOLG,
                             f"'{absage}' darf nicht als Erfolg gelten")

    def test_alle_rueckgaben_des_codes_sind_zugeordnet(self):
        """Gegen die stille Variante: ein Rueckgabewert, den niemand bedacht hat.
        Liest die tatsaechlichen `return "..."` aus cmd_start_game und prueft, dass jeder
        entweder Erfolg ODER eine bekannte Absage ist."""
        import inspect, re
        quelle = inspect.getsource(self.m.cmd_start_game)
        rueckgaben = set(re.findall(r'return "([a-z-]+)"', quelle))
        bekannt = set(self.m.START_ERFOLG) | {"no-ram", "unknown", "rejected", "bad-world",
                                              "wartung"}
        self.assertTrue(rueckgaben, "es muessen Rueckgabewerte gefunden werden")
        self.assertFalse(rueckgaben - bekannt,
                         f"unbedachte Rueckgabe(n): {rueckgaben - bekannt} — Exit-Code pruefen")


class SpielMetriken(unittest.TestCase):
    """Ein schlafendes Spiel und ein kaputtes sehen von aussen identisch aus: beide antworten
    nicht. Die Metriken sollen genau diese Verwechslung aufloesen — deshalb wird hier vor allem
    geprueft, dass der KAPUTTE Zustand auch wirklich als solcher herauskommt. Ein Waechter, der
    nur den Normalfall beschreibt, faellt genau dann aus, wenn man ihn braucht."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.m = load_arbiter(self.tmp)
        self.ordner = os.path.join(self.tmp, "node-exporter")
        os.makedirs(self.ordner)
        self.m.METRICS_FILE = os.path.join(self.ordner, "spiele.prom")
        self.spiel = {"name": "valheim", "kind": "systemd", "service": "valheim-server",
                      "greeter_service": "game-gateway-valheim", "min_free_mb": 2500,
                      "ram_mb": 2000, "idle_timeout_s": 1200,
                      "probe": {"type": "a2s", "ip": "127.0.0.1", "port": 2457}}
        self.h = TickHarness(self.m, games=[self.spiel])
        self.st = self.m.load_state()

    def _schreibe(self, server_laeuft, platzhalter_laeuft, avail=9000):
        self.m.game_active = lambda g: server_laeuft
        self.m.platzhalter_aktiv = lambda g: platzhalter_laeuft
        snap = {"games": {"detail": {}, "bedarf": {}},
                "ram": {"avail_mb": avail, "reserviert_startend_mb": 0}}
        self.m.metriken_schreiben(snap, self.st)
        with open(self.m.METRICS_FILE) as f:
            return f.read()

    def test_weder_server_noch_platzhalter_meldet_nicht_weckbar(self):
        """DER Kernfall. Faellt der Platzhalter aus, waehrend der Server schlaeft, ist das Spiel
        fuer Spieler tot — der Wirt sieht dabei kerngesund aus. Ohne diese Zeile merkt es
        niemand, bis sich jemand beschwert."""
        t = self._schreibe(server_laeuft=False, platzhalter_laeuft=False)
        self.assertIn('spiel_weckbar{spiel="valheim"} 0', t)

    def test_schlafend_aber_mit_platzhalter_gilt_als_weckbar(self):
        """Der Normalfall darf keinen Alarm ausloesen: Server aus, Platzhalter haelt ihn in der
        Serverliste sichtbar und weckt ihn — das ist gesund, nicht kaputt."""
        t = self._schreibe(server_laeuft=False, platzhalter_laeuft=True)
        self.assertIn('spiel_weckbar{spiel="valheim"} 1', t)
        self.assertIn('spiel_server_aktiv{spiel="valheim"} 0', t)
        self.assertIn('spiel_platzhalter_aktiv{spiel="valheim"} 1', t)

    def test_laufender_server_ohne_platzhalter_ist_normal(self):
        """Waehrend der Server laeuft, MUSS der Platzhalter aus sein — sie teilen sich den Port.
        Das darf nicht als Stoerung durchschlagen."""
        t = self._schreibe(server_laeuft=True, platzhalter_laeuft=False)
        self.assertIn('spiel_weckbar{spiel="valheim"} 1', t)

    def test_spiel_ohne_platzhalter_erzeugt_keine_weckbar_zeile(self):
        """Wo es keinen Platzhalter gibt, gibt es auch keine Aussage darueber. Eine erfundene
        1 waere schlimmer als gar keine Zahl: sie sicherte etwas zu, das niemand geprueft hat."""
        self.m.GAMES = [{"name": "solo", "kind": "systemd", "service": "solo-server",
                         "min_free_mb": 1000, "probe": {"type": "a2s", "ip": "127.0.0.1", "port": 1}}]
        t = self._schreibe(server_laeuft=False, platzhalter_laeuft=None)
        self.assertIn('spiel_server_aktiv{spiel="solo"} 0', t)
        self.assertNotIn('spiel_weckbar{spiel="solo"}', t)
        self.assertNotIn('spiel_platzhalter_aktiv{spiel="solo"}', t)

    def test_startbarkeit_folgt_derselben_rechnung_wie_der_precheck(self):
        """Sonst zeigt die Metrik 'startbar', wo der Arbiter Sekunden spaeter absagt."""
        knapp = self._schreibe(False, True, avail=2000)     # unter min_free_mb 2500
        self.assertIn('spiel_startbar{spiel="valheim"} 0', knapp)
        reicht = self._schreibe(False, True, avail=3000)
        self.assertIn('spiel_startbar{spiel="valheim"} 1', reicht)

    def test_ohne_exporter_verzeichnis_wird_nichts_geschrieben(self):
        """Auf Node .18 gibt es keinen node-exporter. Der Export muss sich dort von selbst
        abschalten, ohne dass jemand eine Konfiguration pflegen muss."""
        self.m.METRICS_FILE = os.path.join(self.tmp, "gibt-es-nicht", "spiele.prom")
        self.m.game_active = lambda g: True
        self.m.platzhalter_aktiv = lambda g: True
        self.m.metriken_schreiben({"games": {}, "ram": {"avail_mb": 9000}}, self.st)
        self.assertFalse(os.path.exists(self.m.METRICS_FILE))

    def test_datei_ist_fuer_den_exporter_lesbar(self):
        """Der node-exporter laeuft als eigener Nutzer, der Arbiter als root. Ohne das chmod
        traegt die Datei 0600 und der Exporter meldet still einen Lesefehler — die Metriken
        fehlen dann einfach, was von 'alles ruhig' nicht zu unterscheiden ist."""
        self._schreibe(False, True)
        self.assertTrue(os.stat(self.m.METRICS_FILE).st_mode & 0o004,
                        "der Exporter laeuft unter einem anderen Nutzer und braucht Leserecht")

    def test_schreibfehler_legt_den_arbiter_nicht_lahm(self):
        """Beobachtung darf den Spielbetrieb nie gefaehrden."""
        def kaputt(g): raise RuntimeError("Sensor weg")
        self.m.game_active = kaputt
        self.m.metriken_schreiben({"games": {}, "ram": {"avail_mb": 9000}}, self.st)   # darf nicht werfen


class PlatzhalterNachDemStopp(unittest.TestCase):
    """Nach dem Server-Stopp uebernimmt der Platzhalter den Port und ist von da an der einzige
    Weckweg. Frueher lief sein Start ungeprueft durch: schlug er fehl, war das Spiel weder wach
    noch weckbar — und nichts im Log sagte das.

    Kein TickHarness: der ersetzt game_stop durch eine Attrappe, hier soll aber genau der
    echte Ablauf geprueft werden."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.m = load_arbiter(self.tmp)
        self.m.DRY = False
        self.spiel = {"name": "valheim", "kind": "systemd", "service": "valheim-server",
                      "greeter_service": "game-gateway-valheim"}

    def _lauf(self, greeter_rc):
        self.cmds = []
        def run(cmd, timeout=30):
            self.cmds.append(cmd)
            return (greeter_rc, "", "Fehler" if greeter_rc else "") \
                if "game-gateway-valheim" in cmd else (0, "", "")
        self.m.run = run
        self.m.game_stop(self.spiel)
        return [c for c in self.cmds if "start game-gateway-valheim" in c]

    def test_fehlschlag_wird_wiederholt_und_benannt(self):
        starts = self._lauf(greeter_rc=1)
        self.assertEqual(len(starts), 2, "nach dem ersten Fehlschlag muss ein zweiter Versuch folgen")
        with open(self.m.AUDIT_LOG) as f:
            self.assertIn("weder wach noch weckbar", f.read())

    def test_erfolg_wird_nicht_unnoetig_wiederholt(self):
        starts = self._lauf(greeter_rc=0)
        self.assertEqual(len(starts), 1, "ein geglueckter Start darf nicht doppelt laufen")
        with open(self.m.AUDIT_LOG) as f:
            self.assertNotIn("weder wach noch weckbar", f.read())


class StartVersuchHaeltNicht(unittest.TestCase):
    """Ein Start, der nicht haelt, war bis 2026-09-12 unsichtbar.

    'systemctl start' quittiert bei Type=simple sofort mit rc=0, lange bevor der Server
    steht -- der Rueckgabewert bestaetigt also die Absicht, nicht die Wirkung. Scheiterte
    der Dienst danach (fuenf Fehlstarts in 10 s, dann haelt systemd ihn in 'failed' fest),
    versuchte der Tick es im Minutentakt endlos weiter: kein Zaehler, keine Meldung, keine
    Metrik. Von aussen blieb alles gruen, weil der Platzhalter weiterlief und spiel_weckbar
    damit unveraendert 1 meldete."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.m = load_arbiter(self.tmp)
        self.h = TickHarness(self.m)
        self.st = self.m.load_state()
        # Start, der nicht haelt: der Aufruf wird protokolliert, das Spiel bleibt aus.
        def start_ohne_wirkung(g, world=None):
            self.h.calls.append("game_start:" + g["name"]); return True
        self.m.game_start = start_ohne_wirkung
        self.h.reserviere(self.st, "testgame")

    def starts(self):
        return [c for c in self.h.calls if c.startswith("game_start:")]

    def log(self):
        with open(self.m.AUDIT_LOG) as f: return f.read()

    def test_erster_versuch_laeuft_sofort(self):
        self.h.tick(self.st)
        self.assertEqual(len(self.starts()), 1)
        self.assertEqual(self.h.slot(self.st, "testgame").get("startfehler", 0), 0,
                         "vor Ablauf der Quittungsfrist ist nichts gescheitert")

    def test_fehlversuch_wird_erst_nach_der_quittungsfrist_gezaehlt(self):
        self.h.tick(self.st)
        self.h.tick(self.st)      # direkt danach: der Server darf noch booten
        self.assertEqual(self.h.slot(self.st, "testgame").get("startfehler", 0), 0,
                         "ein bootender Server ist kein Fehlversuch")
        self.h.advance(self.st, self.m.START_QUITTUNG_S + 5)
        self.h.tick(self.st)
        self.assertEqual(self.h.slot(self.st, "testgame")["startfehler"], 1)
        self.assertIn("hat nicht gehalten", self.log())

    def test_activating_zaehlt_nicht_als_fehlversuch(self):
        """Eine Unit, die gerade startet (ExecStartPre laedt ein Update), ist nicht kaputt."""
        self.h.tick(self.st)
        self.h.je_spiel["testgame"] = {"running": False, "players": 0, "unit": "activating"}
        self.h.advance(self.st, self.m.START_QUITTUNG_S + 5)
        self.h.tick(self.st)
        self.assertEqual(self.h.slot(self.st, "testgame").get("startfehler", 0), 0)
        self.assertEqual(len(self.starts()), 1, "waehrend des Starts wird nicht nachgetreten")

    def test_backoff_bremst_erst_ab_dem_dritten_fehlversuch(self):
        """Die ersten Versuche laufen ohne Zusatzwartezeit -- die haeufigste Ursache ist ein
        Port, den der Vorgaenger noch haelt, und die ist nach einer Minute weg."""
        for _ in range(3):
            self.h.advance(self.st, self.m.START_QUITTUNG_S + 5)
            self.h.tick(self.st)
        self.assertEqual(self.h.slot(self.st, "testgame")["startfehler"], 2)
        self.assertEqual(len(self.starts()), 3, "Versuch 1 bis 3 laufen ohne Bremse")
        # Ab hier greift der Backoff: derselbe Abstand loest jetzt KEINEN Start mehr aus.
        self.h.advance(self.st, self.m.START_QUITTUNG_S + 5)
        self.h.tick(self.st)
        self.assertEqual(len(self.starts()), 3, "der vierte Versuch wartet den Backoff ab")
        self.assertIn("Fehlstarts in Folge", self.log())

    def test_backoff_gibt_nie_ganz_auf(self):
        """Nach behobener Ursache muss das Spiel von selbst zurueckkommen."""
        for _ in range(6):
            self.h.advance(self.st, 3600)
            self.h.tick(self.st)
        vorher = len(self.starts())
        self.assertGreaterEqual(vorher, 4, "auch nach vielen Fehlstarts wird weiter versucht")
        self.h.advance(self.st, 3600)
        self.h.tick(self.st)
        self.assertGreater(len(self.starts()), vorher)

    def test_zaehler_faellt_zurueck_sobald_das_spiel_laeuft(self):
        self.h.advance(self.st, self.m.START_QUITTUNG_S + 5); self.h.tick(self.st)
        self.h.advance(self.st, self.m.START_QUITTUNG_S + 5); self.h.tick(self.st)
        self.assertGreater(self.h.slot(self.st, "testgame")["startfehler"], 0)
        self.h.game_running = True
        self.h.tick(self.st)
        slot = self.h.slot(self.st, "testgame")
        self.assertEqual(slot["startfehler"], 0)
        self.assertEqual(slot["naechster_versuch"], 0, "sonst bremst eine behobene Ursache "
                         "den naechsten echten Fehlstart noch aus")
        self.assertIn("laeuft wieder nach", self.log())

    def test_gescheiterte_unit_wird_vor_dem_start_zurueckgesetzt(self):
        """systemd fuehrt ein 'start' auf einer Unit in 'failed' gar nicht erst aus."""
        befehle = []
        def run(cmd, timeout=30):
            befehle.append(cmd); return (0, "", "")
        self.m.run = run
        self.h.je_spiel["testgame"] = {"running": False, "players": 0, "unit": "failed"}
        self.h.tick(self.st)
        self.assertTrue(any("reset-failed" in c for c in befehle),
                        "ohne reset-failed laeuft jeder weitere Startversuch ins Leere")

    def test_unreserviertes_spiel_in_failed_wird_freigeraeumt(self):
        """Der Platzhalter laeuft weiter und meldet 'weckbar' -- also muss der Fehlerzustand
        weg, bevor der naechste Spieler ins Leere weckt."""
        st = self.m.load_state(); st["games"] = {}
        befehle = []
        def run(cmd, timeout=30):
            befehle.append(cmd); return (0, "", "")
        self.m.run = run
        self.h.je_spiel["testgame"] = {"running": False, "players": 0, "unit": "failed"}
        self.h.tick(st)
        self.assertTrue(any("reset-failed" in c for c in befehle))
        self.assertIn("failed", self.log())
        self.assertNotIn("game_start:testgame", self.h.calls,
                         "ungewollt gestartet wird dabei nichts")


class WartungSperre(unittest.TestCase):
    """Waehrend ein Update laeuft, liegt die Installation halb alt und halb neu auf der
    Platte. Wer in diesem Moment weckt, startet einen Server auf halbem Stand. Bei Terraria
    und Factorio reicht dafuer ein Beitrittsversuch am Platzhalter, ohne dass ein Mensch
    beteiligt ist. Geprueft wird deshalb vor allem, dass die Sperre auf ALLEN Wegen greift
    und dass sie von aussen sichtbar ist: eine unsichtbare Sperre ist ein Spiel, das ohne
    erkennbaren Grund nicht mehr startet."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.m = load_arbiter(self.tmp)
        self.h = TickHarness(self.m)
        self.st = self.m.load_state()

    def log(self):
        with open(self.m.AUDIT_LOG) as f:
            return f.read()

    def test_wecken_wird_abgelehnt(self):
        self.m.set_game_wartung(self.st, "testgame", True, "Update Valheim")
        r = self.m.cmd_start_game(self.st, "testgame")
        self.assertEqual(r, "wartung")
        self.assertNotIn("game_start:testgame", self.h.calls,
                         "in Wartung darf kein Weckweg den Server hochziehen")

    def test_absage_nennt_grund_und_ausweg(self):
        """Die Absage geht unveraendert bis in Discord durch. Wer nur 'ABGELEHNT' liest,
        sucht nach einem Schalter, den er nicht kennt."""
        self.m.set_game_wartung(self.st, "testgame", True, "Update Valheim")
        self.m.cmd_start_game(self.st, "testgame")
        text = self.log()
        self.assertIn("Update Valheim", text)
        self.assertIn("--wartung-aus", text)

    def test_absage_ist_kein_erfolg(self):
        """Der Rueckgabewert wird zum Exit-Code. Faellt 'wartung' versehentlich auf die
        Erfolgsseite, zeigt der Bot einen Ladebalken fuer einen Server, der nie kommt."""
        self.assertNotIn("wartung", self.m.START_ERFOLG)

    def test_tick_holt_sich_das_spiel_nicht_zurueck(self):
        """Der gefaehrlichere Weg: die Reservierung bleibt bestehen, waehrend das Werkzeug
        den Server herunterfaehrt. Ohne die Sperre im Tick startet der Arbiter ihn in der
        naechsten Minute mitten im Update wieder."""
        self.h.reserviere(self.st, "testgame")
        self.h.game_running = False
        self.m.set_game_wartung(self.st, "testgame", True, "Update")
        self.h.tick(self.st)
        self.assertNotIn("game_start:testgame", self.h.calls)
        self.assertIn("testgame", self.st["games"], "die Reservierung soll erhalten bleiben")

    def test_nach_dem_ende_startet_der_tick_wieder(self):
        self.h.reserviere(self.st, "testgame")
        self.h.game_running = False
        self.m.set_game_wartung(self.st, "testgame", True, "Update")
        self.h.tick(self.st)
        self.m.set_game_wartung(self.st, "testgame", False)
        self.h.tick(self.st)
        self.assertIn("game_start:testgame", self.h.calls,
                      "eine beendete Wartung muss den Weg wieder freigeben")

    def test_sperre_gilt_nur_fuer_das_genannte_spiel(self):
        zwei = [dict(self.h.game, name="a", service="a"), dict(self.h.game, name="b", service="b")]
        self.m.GAMES = zwei
        self.m.set_game_wartung(self.st, "a", True, "Update")
        self.assertEqual(self.m.cmd_start_game(self.st, "a"), "wartung")
        self.h.je_spiel["b"] = {"running": False, "players": 0}
        self.assertEqual(self.m.cmd_start_game(self.st, "b"), "started")

    def test_wartung_ueberlebt_den_neustart(self):
        """Sie steht in state.json, nicht im Prozess. Ein Arbiter-Neustart mitten im Update
        darf die Sperre nicht verlieren."""
        self.m.set_game_wartung(self.st, "testgame", True, "Update")
        self.m.save_state(self.st)
        neu = self.m.load_state()
        self.assertIsNotNone(self.m.game_in_wartung(neu, "testgame"))

    def test_metrik_meldet_die_sperre(self):
        """Ohne eigene Reihe waere eine vergessene Wartung von aussen nicht von einem
        gesunden schlafenden Spiel zu unterscheiden: spiel_weckbar meldet weiter 1, weil
        der Platzhalter laeuft."""
        ordner = os.path.join(self.tmp, "node-exporter")
        os.makedirs(ordner)
        self.m.METRICS_FILE = os.path.join(ordner, "spiele.prom")
        self.m.set_game_wartung(self.st, "testgame", True, "Update")
        self.st["games"] = {}
        self.h.tick(self.st)
        with open(self.m.METRICS_FILE) as f:
            text = f.read()
        self.assertIn('spiel_wartung{spiel="testgame"} 1', text)
        self.assertIn("spiel_wartung_sekunden", text)

    def test_status_json_zeigt_die_sperre(self):
        """Damit eine Oberflaeche den Weckknopf ausgrauen kann, statt den Nutzer in eine
        Absage laufen zu lassen."""
        self.m.set_game_wartung(self.st, "testgame", True, "Mods tauschen")
        self.st["games"] = {}
        self.h.tick(self.st)
        with open(self.m.STATUS_FILE) as f:
            snap = json.load(f)
        eintrag = snap["games"]["wartung"]["testgame"]
        self.assertEqual(eintrag["grund"], "Mods tauschen")
        # 'seit' gehoert dazu: ohne den Zeitpunkt kann eine Oberflaeche nicht zwischen
        # "seit zwei Minuten in Arbeit" und "seit gestern vergessen" unterscheiden.
        self.assertIsInstance(eintrag["seit"], (int, float))
        self.assertGreater(eintrag["seit"], 0)

    def test_ohne_wartung_bleibt_alles_wie_vorher(self):
        """Die Gegenprobe: ohne gesetzte Sperre darf sich am bisherigen Verhalten nichts
        aendern, weder am Start noch an den Metriken."""
        self.assertIsNone(self.m.game_in_wartung(self.st, "testgame"))
        self.assertEqual(self.m.cmd_start_game(self.st, "testgame"), "started")
        self.assertEqual(self.m.wartung_games(self.st), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
