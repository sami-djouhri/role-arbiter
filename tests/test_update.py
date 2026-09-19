#!/usr/bin/env python3
"""Tests fuer spiel-aktualisieren, ohne SteamCMD, ohne Server, ohne Netz.

Geprueft wird der Teil, der still falsch sein kann: WELCHES Spiel ueberhaupt einen
automatischen Weg hat, ob die Schutzschritte in der richtigen Reihenfolge kommen und ob
die Wartung auch dann wieder faellt, wenn zwischendrin etwas schiefgeht. Der Download
selbst ist nicht die Stelle, an der ein Update gefaehrlich wird.

Aufruf:  python3 tests/test_update.py
"""
import importlib.util, json, os, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
WERKZEUG = next(
    (p for p in (os.path.join(HERE, "..", "arbiter", "spiel-aktualisieren.py"),
                 os.path.join(HERE, "..", "spiel-aktualisieren.py"),
                 "/opt/game-arbiter/spiel-aktualisieren.py") if os.path.isfile(p)),
    os.path.join(HERE, "..", "arbiter", "spiel-aktualisieren.py"))

REGISTRY = {"games": [
    {"name": "valheim", "service": "valheim-server",
     "update": {"kind": "steam", "appid": 896660, "anonym": True, "user": "valheim",
                "steamcmd": "/home/valheim/steamcmd/steamcmd.sh", "dir": "/home/valheim/valheim-server"}},
    {"name": "zomboid", "service": "zomboid-server",
     "update": {"kind": "steam", "appid": 380870, "anonym": True, "user": "zomboid",
                "steamcmd": "/home/zomboid/steamcmd/steamcmd.sh", "dir": "/home/zomboid/pz-server",
                "nach_update": ["sed -i 's/8g/3g/' /home/zomboid/pz-server/ProjectZomboid64.json"]}},
    {"name": "dayz", "service": "dayz-server",
     "update": {"kind": "steam", "appid": 223350, "anonym": False, "user": "dayz",
                "dir": "/home/dayz/dayz-server", "hinweis": "laedt nicht anonym"}},
    {"name": "terraria", "service": "terraria-server",
     "update": {"kind": "manuell", "hinweis": "tModLoader von Hand"}},
    {"name": "factorio", "service": "factorio-server",
     "update": {"kind": "factorio", "user": "factorio", "dir": "/home/factorio/factorio",
                "url": "https://example.invalid/headless"}},
    {"name": "ohnefeld", "service": "ohnefeld-server"},
]}


def load(tmpdir, registry=None):
    """Frische Modul-Instanz mit Registry in einem Wegwerf-Verzeichnis."""
    sys.argv = ["spiel-aktualisieren"]
    spec = importlib.util.spec_from_file_location("upd_under_test", WERKZEUG)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m.BASE = tmpdir
    m.GAMES_JSON = os.path.join(tmpdir, "games.json")
    with open(m.GAMES_JSON, "w") as f:
        json.dump(registry or REGISTRY, f)
    m.PROBE_INTERVALL_S = 0
    m.PROBE_TIMEOUT_S = 1
    return m


class Attrappe:
    """Ersetzt jeden Aussenkontakt und protokolliert, was das Werkzeug tun WOLLTE."""

    def __init__(self, m, laeuft=False, spieler=0, wird_erreichbar=True, update_ok=True):
        self.m = m
        self.schritte = []
        self.laeuft = laeuft
        self.spieler = spieler
        # Ein Zaehler waere hier die falsche Attrappe: bei PROBE_INTERVALL_S=0 fragt das
        # Werkzeug in einer Sekunde tausendfach, und jede noch so hohe Schwelle waere
        # irgendwann erreicht. Der erste Entwurf dieser Datei meldete deshalb Erfolg fuer
        # einen Server, der nie hochkam, und der zugehoerige Test war gruen.
        self.wird_erreichbar = wird_erreichbar
        self.update_ok = update_ok
        self.stand = ["alt", "neu"]

        def arbiter(*args, live=False):
            self.schritte.append(" ".join(args) + (" --live" if live else ""))
            if args[0] == "--wake":
                self.laeuft = True
            if args[0] == "--sleep":
                self.laeuft = False
            return 0, ""

        def zustand(name):
            return {"game": name, "service_active": self.laeuft,
                    "reachable": self.laeuft and self.wird_erreichbar,
                    "players": self.spieler}

        def stand_lesen(g):
            return self.stand[min(len(self.schritte) // 3, len(self.stand) - 1)]

        def update_steam(u, trocken):
            # Das trocken-Flag muss auch die Attrappe beachten, sonst prueft der
            # Trockenlauf-Test gegen eine Attrappe, die immer "getan" meldet.
            self.schritte.append(("plan:" if trocken else "") + "steamcmd:" + str(u["appid"]))
            return self.update_ok, "Success! App"

        def update_factorio(u, trocken):
            self.schritte.append(("plan:" if trocken else "") + "factorio-download")
            return self.update_ok, "entpackt"

        m.arbiter, m.zustand, m.stand_lesen = arbiter, zustand, stand_lesen
        m.update_steam, m.update_factorio = update_steam, update_factorio
        m.os.geteuid = lambda: 0


class WelcheSpieleEinenWegHaben(unittest.TestCase):
    """Die Auswahl ist die Stelle, an der ein Werkzeug still zu viel tut. DayZ laedt nicht
    anonym, Terraria und Minecraft haben gar keinen automatischen Weg: alle drei duerfen
    nie in einem --alle-Lauf landen, sonst scheitert entweder SteamCMD an einer
    Passwortabfrage oder es passiert wortlos nichts."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.m = load(self.tmp)
        self.a = Attrappe(self.m)

    def test_die_auswahl_nennt_nur_automatische_wege(self):
        """Direkt an der Auswahl gemessen, nicht an ihrer Wirkung: dass ein
        durchgerutschtes DayZ spaeter noch einmal abgewiesen wird, ist ein zweites Netz
        und kein Ersatz dafuer, dass die Liste von vornherein stimmt."""
        self.assertEqual(self.m.automatische_spiele(), ["valheim", "zomboid", "factorio"])

    def test_alle_fasst_nur_diese_an(self):
        sys.argv = ["spiel-aktualisieren", "--alle"]
        self.m.main()
        angefasst = [s for s in self.a.schritte if s.startswith(("plan:steamcmd", "plan:factorio"))]
        self.assertEqual(len(angefasst), 3, "valheim, zomboid, factorio und sonst nichts")

    def test_dayz_wird_mit_begruendung_abgelehnt(self):
        rc = self.m.aktualisieren("dayz", live=True)
        self.assertEqual(rc, 3)
        self.assertEqual(self.a.schritte, [], "ein Spiel ohne Weg darf nichts anfassen")

    def test_manueller_weg_wird_abgelehnt(self):
        self.assertEqual(self.m.aktualisieren("terraria", live=True), 3)

    def test_fehlendes_update_feld_wird_abgelehnt(self):
        self.assertEqual(self.m.aktualisieren("ohnefeld", live=True), 3)

    def test_unbekanntes_spiel(self):
        self.assertEqual(self.m.aktualisieren("gibtsnicht", live=True), 3)


class NiemandWirdGestoert(unittest.TestCase):
    """Die Owner-Regel gilt auch fuer ein Update: wer spielt, wird nicht gestoert."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.m = load(self.tmp)

    def test_spieler_online_bricht_ab(self):
        a = Attrappe(self.m, laeuft=True, spieler=2)
        rc = self.m.aktualisieren("valheim", live=True)
        self.assertEqual(rc, 4)
        self.assertEqual(a.schritte, [], "kein Stopp, keine Wartung, kein Update")

    def test_leerer_server_wird_vorher_gestoppt(self):
        a = Attrappe(self.m, laeuft=True, spieler=0)
        self.m.aktualisieren("valheim", live=True)
        self.assertIn("--sleep valheim --live", a.schritte)
        self.assertLess(a.schritte.index("--sleep valheim --live"),
                        a.schritte.index("steamcmd:896660"),
                        "erst herunterfahren, dann die Dateien tauschen")


class SchutzschritteInDerRichtigenReihenfolge(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.m = load(self.tmp)
        self.a = Attrappe(self.m)

    def test_wartung_kommt_vor_dem_update(self):
        self.m.aktualisieren("valheim", live=True)
        s = self.a.schritte
        self.assertLess(s.index("--wartung-an valheim --grund Update laeuft (spiel-aktualisieren)"),
                        s.index("steamcmd:896660"))

    def test_snapshot_kommt_vor_dem_update(self):
        self.m.aktualisieren("valheim", live=True)
        s = self.a.schritte
        self.assertLess(s.index("--snapshot valheim --live"), s.index("steamcmd:896660"),
                        "ein Schnappschuss nach dem Update sichert den neuen Stand, nicht den alten")

    def test_wartung_faellt_auch_nach_einem_fehler(self):
        """Der wichtigste Test: eine vergessene Wartung ist ein Spiel, das ohne
        erkennbaren Grund nicht mehr startet."""
        self.a.update_ok = False
        rc = self.m.aktualisieren("valheim", live=True)
        self.assertEqual(rc, 1)
        self.assertTrue(any(s.startswith("--wartung-aus") for s in self.a.schritte),
                        "die Wartung muss auch im Fehlerfall wieder fallen")

    def test_wartung_faellt_auch_bei_einem_absturz(self):
        """Nicht nur bei rc != 0: auch wenn mitten im Lauf etwas wirft."""
        def knallt(u, trocken):
            raise RuntimeError("Platte voll")
        self.m.update_steam = knallt
        with self.assertRaises(RuntimeError):
            self.m.aktualisieren("valheim", live=True)
        self.assertTrue(any(s.startswith("--wartung-aus") for s in self.a.schritte))

    def test_wartung_faellt_vor_dem_probestart(self):
        """Sonst lehnt die eigene Sperre den eigenen Probestart ab."""
        self.m.aktualisieren("valheim", live=True)
        s = self.a.schritte
        self.assertLess(s.index("--wartung-aus valheim"), s.index("--wake valheim --live"))

    def test_nacharbeit_laeuft_nach_dem_update(self):
        rufe = []
        self.m.lauf = lambda cmd, timeout=1800, eingabe=None: (rufe.append(cmd) or (0, ""))
        self.m.aktualisieren("zomboid", live=True)
        self.assertTrue(any("ProjectZomboid64.json" in " ".join(c) for c in rufe),
                        "ohne die Nacharbeit steht der Java-Heap wieder auf 8g")


class AusgangszustandWirdWiederhergestellt(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.m = load(self.tmp)

    def test_schlafender_server_schlaeft_danach_wieder(self):
        a = Attrappe(self.m, laeuft=False)
        self.m.aktualisieren("valheim", live=True)
        self.assertEqual(a.schritte[-1], "--sleep valheim --live",
                         "ein Update darf kein Spiel dauerhaft wachlassen")

    def test_laufender_server_bleibt_danach_laufen(self):
        a = Attrappe(self.m, laeuft=True)
        self.m.aktualisieren("valheim", live=True)
        self.assertNotEqual(a.schritte[-1], "--sleep valheim --live")

    def test_probestart_ohne_erfolg_meldet_fehler(self):
        """Ein Update, dessen Server nicht mehr hochkommt, ist schlechter als keines.
        Es darf nicht als Erfolg zurueckkommen."""
        a = Attrappe(self.m, wird_erreichbar=False)
        rc = self.m.aktualisieren("valheim", live=True)
        self.assertEqual(rc, 1)

    def test_trockenlauf_fasst_nichts_an(self):
        a = Attrappe(self.m)
        rc = self.m.aktualisieren("valheim", live=False)
        self.assertEqual(rc, 0)
        self.assertEqual([s for s in a.schritte if "--live" in s or s.startswith("steamcmd")], [])


class StandLesen(unittest.TestCase):
    """stand_lesen liest dieselbe Quelle wie der Waechter auf host. Liest es daneben,
    meldet das Werkzeug 'Stand unveraendert' fuer ein Update, das gelaufen ist."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.m = load(self.tmp)

    def test_buildid_aus_dem_steam_manifest(self):
        d = os.path.join(self.tmp, "valheim", "steamapps")
        os.makedirs(d)
        with open(os.path.join(d, "appmanifest_896660.acf"), "w") as f:
            f.write('"AppState"\n{\n\t"appid"\t\t"896660"\n\t"buildid"\t\t"25390671"\n}\n')
        g = {"update": {"kind": "steam", "appid": 896660, "dir": os.path.join(self.tmp, "valheim")}}
        self.assertEqual(self.m.stand_lesen(g), "25390671")

    def test_fehlendes_manifest_gibt_none_statt_zu_werfen(self):
        g = {"update": {"kind": "steam", "appid": 1, "dir": "/gibt/es/nicht"}}
        self.assertIsNone(self.m.stand_lesen(g))

    def test_factorio_version_aus_info_json(self):
        d = os.path.join(self.tmp, "factorio", "data", "base")
        os.makedirs(d)
        with open(os.path.join(d, "info.json"), "w") as f:
            json.dump({"name": "base", "version": "2.0.77"}, f)
        g = {"update": {"kind": "factorio", "dir": os.path.join(self.tmp, "factorio")}}
        self.assertEqual(self.m.stand_lesen(g), "2.0.77")


if __name__ == "__main__":
    unittest.main(verbosity=2)
