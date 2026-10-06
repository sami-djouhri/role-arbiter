#!/usr/bin/env python3
"""Selbstheilung (Owner-Vorgabe 2026-10-04): Ansage im Spiel, Haenger-Neustart, Platzhalter.

Alles mit Attrappen: run/act/ankuendigen/game_stop/game_start sind ersetzt, die Uhr wird
von Hand vorgestellt. Kein Test startet, stoppt oder ruft systemctl auf irgendeinem Wirt.
"""
import importlib.util, os, sys, tempfile, unittest

ARBITER_PY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "arbiter", "arbiter.py")


class Uhr:
    def __init__(self): self.t = 1_800_000_000.0
    def time(self): return self.t
    def sleep(self, s): self.t += s


def lade():
    tmp = tempfile.mkdtemp()
    sys.argv = ["arbiter.py"]
    spec = importlib.util.spec_from_file_location("arb_heilung_test", ARBITER_PY)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m.BASE = tmp
    m.STATE_FILE = os.path.join(tmp, "state.json")
    m.METRICS_FILE = os.path.join(tmp, "spiele.prom")
    m.DRY = False
    m.LOG = []
    m.audit = lambda msg: m.LOG.append(msg)
    m.audit_status = lambda st, k, key, msg: m.LOG.append(msg)
    m.befehle = []
    m.run = lambda cmd, timeout=30: (m.befehle.append(cmd), (0, "", ""))[1]
    m.act = lambda desc, cmd, timeout=90: (m.befehle.append(cmd), True)[1]
    m.aktionen = []
    m.game_stop = lambda g: m.aktionen.append(("stop", g["name"])) or True
    m.game_start = lambda g, world=None: m.aktionen.append(("start", g["name"], world)) or True
    m.game_active = lambda g: False
    m.ansagen = []
    m.ankuendigen = lambda g, text: (m.ansagen.append(text), bool(g.get("ansage")))[1]
    # Eigene Uhr NUR fuer dieses Modul: m.time IST das globale time-Modul, ein Ersetzen von
    # m.time.time wuerde jeden anderen Test mitnehmen (siehe test_timers).
    uhr = Uhr()
    class _Zeit:
        time = staticmethod(uhr.time)
        sleep = staticmethod(uhr.sleep)
    m.time = _Zeit
    m.uhr = uhr
    m.GAMES = [{"name": "tg", "kind": "systemd", "service": "tg-server",
                "probe": {"type": "a2s", "ip": "127.0.0.1", "port": 1},
                "ansage": {"art": "tmux", "nutzer": "tg"}}]
    return m


class Haenger(unittest.TestCase):
    def setUp(self):
        self.m = lade()
        self.st = {"games": {"tg": {"since": self.m.uhr.t - 3600, "world": "w1"}}}
        self.slot = self.st["games"]["tg"]
        self.g = self.m.GAMES[0]

    def tick(self, gp, minuten=1):
        self.m.uhr.t += 60 * minuten
        return self.m._haenger_pruefen(self.st, self.g, self.slot, gp)

    def test_antwortender_server_bleibt_unberuehrt(self):
        for _ in range(20):
            self.assertFalse(self.tick(3))
        self.assertEqual(self.m.aktionen, []); self.assertEqual(self.m.ansagen, [])

    def test_volle_zeitleiste(self):
        self.tick(-1)                                  # beobachten
        for _ in range(4): self.tick(-1)               # schweigt < 5 min
        self.assertEqual(self.m.ansagen, [])
        self.tick(-1)                                  # 5 min erreicht -> Ankuendigung
        self.assertEqual(len(self.m.ansagen), 1)
        self.assertIn("5 Minuten", self.m.ansagen[0])
        self.assertEqual(self.m.aktionen, [])
        for _ in range(4): self.tick(-1)               # Vorwarnzeit laeuft
        self.assertEqual(self.m.aktionen, [])
        self.tick(-1)                                  # 5 min Vorwarnung um -> 10 s, Neustart
        self.assertIn("10 Sekunden", self.m.ansagen[-1])
        self.assertEqual(self.m.aktionen, [("stop", "tg"), ("start", "tg", "w1")])
        self.assertEqual(len(self.st["heilungen"]["tg"]), 1)
        self.assertIsNone(self.slot.get("heilung"))

    def test_antwortet_waehrend_der_vorwarnung_wieder(self):
        for _ in range(6): self.tick(-1)
        self.assertEqual(len(self.m.ansagen), 1)
        self.assertFalse(self.tick(0))                 # 0 Spieler ist eine Antwort
        self.assertIn("kein Neustart", self.m.ansagen[-1])
        for _ in range(10): self.tick(0)
        self.assertEqual(self.m.aktionen, [])

    def test_startfrist_wird_abgewartet(self):
        self.slot["since"] = self.m.uhr.t
        for _ in range(9): self.assertFalse(self.tick(-1))
        self.assertEqual(self.m.ansagen, [])

    def test_wartung_verhindert_heilung(self):
        self.st["wartung"] = {"tg": {"seit": self.m.uhr.t, "grund": "Update"}}
        for _ in range(20): self.tick(-1)
        self.assertEqual(self.m.aktionen, []); self.assertEqual(self.m.ansagen, [])

    def test_ohne_ansage_kanal_trotzdem_neustart(self):
        self.g.pop("ansage")
        for _ in range(11): self.tick(-1)
        self.assertEqual(self.m.aktionen, [("stop", "tg"), ("start", "tg", "w1")])

    def test_tcp_probe_null_heisst_port_zu(self):
        self.g["probe"] = {"type": "tcp", "ip": "127.0.0.1", "port": 1}
        for _ in range(11): self.tick(0)
        self.assertEqual(self.m.aktionen[0], ("stop", "tg"))


class Ansage(unittest.TestCase):
    """Der echte ankuendigen() mit ersetztem run(): was ginge an den Wirt?"""
    def setUp(self):
        self.m = lade()
        spec = importlib.util.spec_from_file_location("arb_ansage_echt", ARBITER_PY)
        echt = importlib.util.module_from_spec(spec); sys.argv = ["arbiter.py"]
        spec.loader.exec_module(echt)
        self.ank = echt.ankuendigen
        echt.DRY = False
        self.befehle = []
        echt.run = lambda cmd, timeout=30: (self.befehle.append(cmd), (0, "", ""))[1]
        echt.audit = lambda msg: None

    def test_tmux_als_dienstnutzer_mit_literalem_text(self):
        g = {"name": "z", "kind": "systemd", "ansage": {"art": "tmux", "nutzer": "zomboid",
                                                        "befehl": 'servermsg "{text}"'}}
        self.assertTrue(self.ank(g, "Neustart in 10 Sekunden"))
        c = self.befehle[0]
        self.assertIn("runuser -u zomboid -- tmux -L zomboid send-keys -t main -l", c)
        self.assertIn("servermsg \"Neustart in 10 Sekunden\"", c.replace("'", ""))
        self.assertTrue(c.startswith("timeout 5 "))

    def test_fifo_mit_timeout(self):
        g = {"name": "t", "kind": "systemd", "ansage": {"art": "fifo", "pfad": "/home/terraria/server.in"}}
        self.assertTrue(self.ank(g, "Server reagiert nicht"))
        self.assertTrue(self.befehle[0].startswith("timeout 5 sh -c"))
        self.assertIn("/home/terraria/server.in", self.befehle[0])

    def test_gefaehrlicher_text_wird_nicht_gesendet(self):
        g = {"name": "t", "kind": "systemd", "ansage": {"art": "fifo", "pfad": "/x"}}
        for boese in ("a; rm -rf /", "$(id)", "`id`", "zeile\nzwei", 'zitat"ende', ""):
            self.assertFalse(self.ank(g, boese), boese)
        self.assertEqual(self.befehle, [])

    def test_kein_kanal(self):
        self.assertFalse(self.ank({"name": "v", "kind": "systemd"}, "Hallo"))


class Platzhalter(unittest.TestCase):
    def setUp(self):
        self.m = lade()
        self.m.GAMES = [{"name": "tg", "kind": "systemd", "service": "tg-server",
                         "greeter_service": "tg-greeter"}]
        self.lage = {"tg-server": "inactive", "tg-greeter": "inactive"}
        def run(cmd, timeout=30):
            self.m.befehle.append(cmd)
            for unit, z in self.lage.items():
                if cmd.endswith("is-active %s" % unit): return 0, z, ""
            return 0, "", ""
        self.m.run = run

    def starts(self):
        return [c for c in self.m.befehle if "systemctl start" in c]

    def test_stehender_platzhalter_wird_nachgestartet_mit_abstand(self):
        st = {}
        self.m.platzhalter_heilen(st)
        self.assertEqual(self.starts(), ["systemctl start tg-greeter"])
        self.m.uhr.t += 60
        self.m.platzhalter_heilen(st)
        self.assertEqual(len(self.starts()), 1)        # Abstand 5 min
        self.m.uhr.t += 300
        self.m.platzhalter_heilen(st)
        self.assertEqual(len(self.starts()), 2)

    def test_nicht_waehrend_server_startet_oder_wartung(self):
        self.lage["tg-server"] = "activating"
        self.m.platzhalter_heilen({})
        self.lage["tg-server"] = "inactive"
        self.m.platzhalter_heilen({"wartung": {"tg": {"seit": 0}}})
        self.m.platzhalter_heilen({"games": {"tg": {}}})
        self.assertEqual(self.starts(), [])

    def test_laufender_platzhalter_bleibt(self):
        self.lage["tg-greeter"] = "active"
        self.m.platzhalter_heilen({})
        self.assertEqual(self.starts(), [])


class EinspielenBeiLaufendemServer(unittest.TestCase):
    def setUp(self):
        self.m = m = lade()
        m.GAMES[0]["multi_world"] = True
        m._game_running = lambda name: True
        m._valid_snapfile = lambda name, rel: "/tmp/x.tar.gz"
        m._save_spec = lambda name: {"parent": "/x"}
        m._snap_ctx = lambda name: m.GAMES[0]
        m.world_info = lambda name: ("aktiv", ["aktiv", "andere"])
        m.cmd_snapshot = lambda *a, **k: 0
        self.st = {"games": {"tg": {"since": 0}}}

    def test_ohne_laufend_abgelehnt_und_nichts_angefasst(self):
        self.assertEqual(self.m.cmd_restore(self.st, "tg", "manuell/tg/andere-20261001-000000.tar.gz"), 4)
        self.assertEqual(self.m.aktionen, []); self.assertEqual(self.m.ansagen, [])

    def test_mit_laufend_ansage_stopp_einspielen_start_mit_alter_welt(self):
        rc = self.m.cmd_restore(self.st, "tg", "manuell/tg/andere-20261001-000000.tar.gz", laufend=True)
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.m.ansagen), 2)
        self.assertIn("60 Sekunden", self.m.ansagen[0])
        self.assertIn("10 Sekunden", self.m.ansagen[1])
        self.assertEqual(self.m.aktionen, [("stop", "tg"), ("start", "tg", "aktiv")])
        self.assertTrue(any("tar xzf" in b for b in self.m.befehle))
        # Der Stopp kommt vor dem Entpacken: sonst schriebe der Server ueber den Stand.


if __name__ == "__main__":
    unittest.main()
