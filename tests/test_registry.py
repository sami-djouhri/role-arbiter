#!/usr/bin/env python3
"""Die Spiele-Registry erfindet keine Spiele.

Bis 2026-10-04 fiel eine leere oder unlesbare games.json auf eine eingebaute Vorgabe
zurueck: DayZ in LXC 204. Auf Node .18 ist die Registry seit dem Umzug absichtlich leer,
und die Nummer 204 gehoert inzwischen dem Monitoring-Gast. Der Arbiter dort hielt den
zweiten Beobachter deshalb fuer einen DayZ-Server. Diese Tests halten fest, dass jeder
dieser Faelle eine leere Registry ergibt und nichts sonst.
"""
import importlib.util, json, os, sys, tempfile, unittest

ARBITER_PY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "arbiter", "arbiter.py")


def lade(tmpdir):
    sys.argv = ["arbiter.py"]
    spec = importlib.util.spec_from_file_location("arb_registry_test", ARBITER_PY)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m.BASE = tmpdir
    m.METRICS_FILE = os.path.join(tmpdir, "spiele.prom")
    return m


class RegistryErfindetNichts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.m = lade(self.tmp)

    def schreibe(self, inhalt):
        with open(os.path.join(self.tmp, "games.json"), "w") as f:
            f.write(inhalt)

    def test_leere_liste_bleibt_leer(self):
        self.schreibe(json.dumps({"games": []}))
        self.assertEqual(self.m.load_games(), [])

    def test_fehlende_datei_ergibt_leere_registry(self):
        self.assertEqual(self.m.load_games(), [])

    def test_kaputte_datei_ergibt_leere_registry(self):
        self.schreibe("{ kein json")
        self.assertEqual(self.m.load_games(), [])

    def test_games_ist_keine_liste(self):
        self.schreibe(json.dumps({"games": {"dayz": {}}}))
        self.assertEqual(self.m.load_games(), [])

    def test_kein_lxc_204_irgendwo(self):
        for inhalt in ('{"games": []}', "{ kaputt"):
            self.schreibe(inhalt)
            namen = [g.get("name") for g in self.m.load_games()]
            self.assertNotIn("dayz", namen)
        self.assertFalse(hasattr(self.m, "_DEFAULT_GAMES"))

    def test_echte_liste_unveraendert(self):
        spiele = [{"name": "terraria", "kind": "systemd", "service": "terraria-server"}]
        self.schreibe(json.dumps({"games": spiele}))
        self.assertEqual(self.m.load_games(), spiele)


class NightlyJedesSpielEinmal(unittest.TestCase):
    def test_minecraft_in_registry_wird_nur_einmal_gesichert(self):
        m = lade(tempfile.mkdtemp())
        m.GAMES = [{"name": "terraria"}, {"name": "minecraft"}]
        m._save_spec = lambda name: {"parent": "/x"}
        m.game_multi_world = lambda name: False
        aufrufe = []
        m.cmd_snapshot = lambda name, **kw: aufrufe.append(name) or 0
        m.cmd_snapshot_all()
        self.assertEqual(sorted(aufrufe), ["minecraft", "terraria"])


if __name__ == "__main__":
    unittest.main()
