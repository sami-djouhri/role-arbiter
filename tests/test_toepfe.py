#!/usr/bin/env python3
"""Sicherungs-Toepfe, Tageslimits und Owner-Rechte (Owner-Vorgabe 2026-10-04).

Die Zusage an den Owner lautet: jede Welt ist mindestens 60 Tage zurueckholbar, und
kein Admin kann das zerstoeren. Diese Tests pruefen genau die Wege, auf denen das
brechen koennte: eine Flut von Admin-Sicherungen, ein Loeschversuch ohne Owner, ein
Nachtlauf, der in place schreibt und damit alle Tagesstaende mitaendert, und zwei
Welten mit aehnlichem Namen, die sich gegenseitig wegraeumen.

Laeuft ohne Wirt: echte tar-Archive in einem Wegwerf-Verzeichnis, host-natives Spiel.
"""
import importlib.util, json, os, sys, tempfile, time, unittest
from datetime import datetime, timedelta

ARBITER_PY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "arbiter", "arbiter.py")


def lade():
    tmp = tempfile.mkdtemp()
    sys.argv = ["arbiter.py"]
    spec = importlib.util.spec_from_file_location("arb_toepfe_test", ARBITER_PY)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m.BASE = tmp
    m.GS_DIR = os.path.join(tmp, "gs")
    m.MANUAL_DIR = os.path.join(m.GS_DIR, "manual")
    m.WORLDS_FILE = os.path.join(tmp, "worlds.json")
    m.AUDIT_LOG = os.path.join(tmp, "audit.log")
    m.METRICS_FILE = os.path.join(tmp, "spiele.prom")
    m.STATE_FILE = os.path.join(tmp, "state.json")
    m.LOG = []
    m.audit = lambda msg: m.LOG.append(msg)
    welt = os.path.join(tmp, "spiel")
    os.makedirs(os.path.join(welt, "worlds"))
    m.SPIELORT = welt
    m.GAMES = [{"name": "tg", "kind": "systemd", "service": "tg-server", "multi_world": True,
                "save": {"parent": welt, "world_items": ["worlds/{w}.wld"], "items": []}}]
    return m


def schreibe_welt(m, wid, inhalt="x", mtime=None):
    p = os.path.join(m.SPIELORT, "worlds", wid + ".wld")
    with open(p, "w") as f:
        f.write(inhalt)
    if mtime:
        os.utime(p, (mtime, mtime))


def welten(m, *ids):
    m.save_worlds({"tg": {"active": ids[0], "worlds": [{"id": i, "label": i} for i in ids]}})


def dateien(m, topf, spiel="tg"):
    try:
        return sorted(f for f in os.listdir(os.path.join(m.GS_DIR, topf, spiel)) if f.endswith(".tar.gz"))
    except OSError:
        return []


def lege_an(m, topf, token, tage_zurueck, spiel="tg"):
    """Eine Sicherungsdatei mit Datum in der Vergangenheit, ohne echten Inhalt."""
    d = os.path.join(m.GS_DIR, topf, spiel)
    os.makedirs(d, exist_ok=True)
    ts = (datetime.now() - timedelta(days=tage_zurueck)).strftime("%Y%m%d-%H%M%S")
    p = os.path.join(d, "%s-%s.tar.gz" % (token, ts))
    open(p, "w").close()
    return os.path.basename(p)


class AdminSicherungen(unittest.TestCase):
    def setUp(self):
        self.m = lade(); welten(self.m, "solo"); schreibe_welt(self.m, "solo")

    def test_drei_am_tag_dann_abgelehnt(self):
        for _ in range(3):
            self.assertEqual(self.m.cmd_snapshot("tg", world="solo"), 0)
            time.sleep(1.01)       # Zeitstempel im Namen hat Sekundenaufloesung
        self.assertEqual(self.m.cmd_snapshot("tg", world="solo"), self.m.RC_LIMIT)
        self.assertEqual(len(dateien(self.m, "manuell")), 3)

    def test_owner_ist_vom_limit_ausgenommen(self):
        for _ in range(3):
            lege_an(self.m, "manuell", "solo", 0)
            time.sleep(1.01)
        self.assertEqual(self.m.cmd_snapshot("tg", world="solo", owner=True), 0)

    def test_gestern_zaehlt_nicht(self):
        for i in range(5):
            lege_an(self.m, "manuell", "solo", 1 + i)
        self.assertEqual(self.m.sicherungen_heute("tg", "solo"), 0)
        self.assertEqual(self.m.cmd_snapshot("tg", world="solo"), 0)

    def test_rotation_manuell_laesst_taeglich_in_ruhe(self):
        for i in range(60):
            lege_an(self.m, "taeglich", "solo", 100 + i)
        for i in range(35):
            lege_an(self.m, "manuell", "solo", 1 + i)
        self.assertEqual(self.m.cmd_snapshot("tg", world="solo"), 0)
        self.assertEqual(len(dateien(self.m, "manuell")), 30)
        self.assertEqual(len(dateien(self.m, "taeglich")), 60)

    def test_aehnliche_weltnamen_raeumen_sich_nicht_gegenseitig_weg(self):
        welten(self.m, "solo", "solo-2")
        for i in range(30):
            lege_an(self.m, "manuell", "solo-2", 1 + i)
        for i in range(30):
            lege_an(self.m, "manuell", "solo", 1 + i)
        self.assertEqual(self.m.cmd_snapshot("tg", world="solo"), 0)
        self.assertEqual(len([f for f in dateien(self.m, "manuell") if f.startswith("solo-2-")]), 30)


class Tagesstaende(unittest.TestCase):
    def setUp(self):
        self.m = lade(); welten(self.m, "solo")
        schreibe_welt(self.m, "solo", "v1", mtime=time.time() - 3600)

    def test_unveraenderte_welt_ergibt_keinen_neuen_tagesstand(self):
        self.assertEqual(self.m.cmd_snapshot("tg", world="solo", nightly=True), 0)
        time.sleep(1.01)
        self.assertEqual(self.m.cmd_snapshot("tg", world="solo", nightly=True), 0)
        self.assertEqual(len(dateien(self.m, "taeglich")), 1)

    def test_geaenderte_welt_ergibt_neuen_tagesstand_und_der_alte_bleibt_gleich(self):
        self.m.cmd_snapshot("tg", world="solo", nightly=True)
        erster = os.path.join(self.m.GS_DIR, "taeglich", "tg", dateien(self.m, "taeglich")[0])
        sig_vorher = self.m._tar_signatur(erster)
        time.sleep(1.01)
        schreibe_welt(self.m, "solo", "v2-laenger")
        self.m.cmd_snapshot("tg", world="solo", nightly=True)
        self.assertEqual(len(dateien(self.m, "taeglich")), 2)
        # ★ Der Kern: das feste Archiv wurde ersetzt, der verlinkte Vortag nicht veraendert.
        self.assertEqual(self.m._tar_signatur(erster), sig_vorher)
        fest = os.path.join(self.m.GS_DIR, "tg", "solo.tar.gz")
        neuester = os.path.join(self.m.GS_DIR, "taeglich", "tg", dateien(self.m, "taeglich")[-1])
        self.assertEqual(os.stat(fest).st_ino, os.stat(neuester).st_ino)
        self.assertNotEqual(os.stat(fest).st_ino, os.stat(erster).st_ino)

    def test_der_61_stand_verdraengt_nur_den_aeltesten(self):
        namen = [lege_an(self.m, "taeglich", "solo", 200 - i) for i in range(60)]
        self.m.cmd_snapshot("tg", world="solo", nightly=True)
        jetzt = dateien(self.m, "taeglich")
        self.assertEqual(len(jetzt), 60)
        self.assertNotIn(namen[0], jetzt)
        self.assertIn(namen[1], jetzt)


class NurOwnerLoescht(unittest.TestCase):
    def setUp(self):
        self.m = lade(); welten(self.m, "solo", "zweite"); schreibe_welt(self.m, "solo")

    def test_sicherung_loeschen_ohne_owner_abgelehnt(self):
        f = lege_an(self.m, "taeglich", "solo", 3)
        rel = "taeglich/tg/" + f
        self.assertEqual(self.m.cmd_delete_snapshot("tg", rel), self.m.RC_NUR_OWNER)
        self.assertIn(f, dateien(self.m, "taeglich"))
        self.assertEqual(self.m.cmd_delete_snapshot("tg", rel, owner=True), 0)
        self.assertNotIn(f, dateien(self.m, "taeglich"))

    def test_welt_loeschen_ohne_owner_abgelehnt(self):
        self.assertEqual(self.m.cmd_delete_world({}, "tg", "zweite"), self.m.RC_NUR_OWNER)
        self.assertEqual(self.m.world_info("tg")[1], ["solo", "zweite"])


class WeltAnlegen(unittest.TestCase):
    def setUp(self):
        self.m = lade(); welten(self.m, "solo")

    def test_eine_neue_welt_je_tag_fuer_alle_zusammen(self):
        self.assertEqual(self.m.cmd_create_world("tg", "eins", wer="anna"), "created")
        self.assertEqual(self.m.cmd_create_world("tg", "zwei", wer="bert"), "limit")
        self.assertEqual(self.m.cmd_create_world("tg", "zwei", wer="owner", owner=True), "created")

    def test_ersteller_steht_an_der_welt(self):
        self.m.cmd_create_world("tg", "eins", wer="anna")
        meta = self.m._welt_meta("tg")["eins"]
        self.assertEqual(meta["created_by"], "anna")
        self.assertTrue(meta["created"].startswith(datetime.now().strftime("%Y-%m-%d")))

    def test_loeschen_setzt_das_tageslimit_nicht_zurueck(self):
        self.m.cmd_create_world("tg", "eins", wer="anna")
        w = self.m.load_worlds()
        w["tg"]["worlds"] = [x for x in w["tg"]["worlds"] if x["id"] != "eins"]
        self.m.save_worlds(w)
        self.assertEqual(self.m.cmd_create_world("tg", "zwei", wer="anna"), "limit")


class Aufraeumen(unittest.TestCase):
    def test_geloescht_nach_30_tagen_weg(self):
        m = lade()
        alt = lege_an(m, "geloescht", "weg", 31)
        jung = lege_an(m, "geloescht", "weg", 29)
        m._geloescht_aufraeumen()
        self.assertEqual(dateien(m, "geloescht"), [jung])
        self.assertNotIn(alt, dateien(m, "geloescht"))

    def test_altlayout_wird_einsortiert(self):
        m = lade()
        d = os.path.join(m.MANUAL_DIR, "tg")
        os.makedirs(d)
        for f in ("solo-20260901-101010.tar.gz", "prerestore-solo-20260902-101010.tar.gz",
                  "deleted-alt-20260903-101010.tar.gz"):
            open(os.path.join(d, f), "w").close()
        self.assertEqual(m._toepfe_einsortieren(), 3)
        self.assertEqual(dateien(m, "manuell"), ["solo-20260901-101010.tar.gz"])
        self.assertEqual(dateien(m, "vor-einspielen"), ["solo-20260902-101010.tar.gz"])
        self.assertEqual(dateien(m, "geloescht"), ["alt-20260903-101010.tar.gz"])
        self.assertFalse(os.path.exists(m.MANUAL_DIR))
        self.assertEqual(m._toepfe_einsortieren(), 0)     # zweimal ist harmlos


if __name__ == "__main__":
    unittest.main()
