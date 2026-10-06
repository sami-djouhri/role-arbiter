#!/usr/bin/env python3
"""wake-bridge: Loeschen nur mit dem Owner-Token, Ersteller wird durchgereicht.

Startet die echte Bridge gegen einen Stellvertreter-Arbiter, der nur seine Argumente
zurueckgibt. So ist sichtbar, was die Bridge dem Arbiter WIRKLICH uebergibt, ohne dass
irgendein Spiel oder eine Sicherung angefasst wird.
"""
import json, os, socket, subprocess, sys, tempfile, time, unittest, urllib.request, urllib.error

# Beim Import festgehalten: unittest laedt alle Module vor dem ersten Test, das echte
# time.sleep ist hier also noch unberuehrt.
_WARTE = time.sleep

BRIDGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "arbiter", "wake-bridge.py")

STUB = r'''#!/usr/bin/env python3
import json, sys
if "--list-games" in sys.argv:
    print(json.dumps([{"name": "tg"}])); sys.exit(0)
print("ARGS " + " ".join(sys.argv[1:]))
'''


def freier_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


class BridgeRechte(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        stub = os.path.join(cls.tmp, "arbiter.py")
        with open(stub, "w") as f: f.write(STUB)
        with open(os.path.join(cls.tmp, "wake.token"), "w") as f: f.write("admintok")
        with open(os.path.join(cls.tmp, "wake.owner.token"), "w") as f: f.write("ownertok")
        cls.port = freier_port()
        env = dict(os.environ, ARBITER=stub, WAKE_PORT=str(cls.port), WAKE_BIND="127.0.0.1")
        cls.proc = subprocess.Popen([sys.executable, BRIDGE], env=env,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # Frist statt Zaehlschleife: test_timers ersetzt time.sleep zeitweise durch ein
        # No-op, eine Schleife "50 mal 0,1 s" waere dann in Mikrosekunden durch.
        frist = time.monotonic() + 15
        while time.monotonic() < frist:
            try:
                socket.create_connection(("127.0.0.1", cls.port), timeout=0.2).close(); break
            except OSError:
                _WARTE(0.1)

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate(); cls.proc.wait(5)

    def post(self, pfad, token, body=None, wer=None):
        req = urllib.request.Request("http://127.0.0.1:%d%s" % (self.port, pfad), method="POST",
                                     data=json.dumps(body or {}).encode())
        req.add_header("Authorization", "Bearer " + token)
        req.add_header("Content-Type", "application/json")
        if wer: req.add_header("X-Wer", wer)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def args(self, antwort):
        return " ".join(antwort.get("log") or [])

    def test_sicherung_loeschen_mit_admin_token_403(self):
        code, _ = self.post("/snapshots/tg/delete", "admintok", {"file": "taeglich/tg/a-20261001-000000.tar.gz"})
        self.assertEqual(code, 403)

    def test_welt_loeschen_mit_admin_token_403(self):
        code, _ = self.post("/worlds/tg/delete", "admintok", {"id": "solo"})
        self.assertEqual(code, 403)

    def test_owner_darf_loeschen_und_owner_flag_kommt_an(self):
        code, a = self.post("/snapshots/tg/delete", "ownertok", {"file": "taeglich/tg/a-20261001-000000.tar.gz"})
        self.assertEqual(code, 200)
        self.assertIn("--owner", self.args(a))

    def test_admin_sicherung_ohne_owner_flag_mit_wer(self):
        code, a = self.post("/snapshot/tg?world=solo", "admintok", wer="anna")
        self.assertEqual(code, 200)
        self.assertNotIn("--owner", self.args(a))
        self.assertIn("--wer anna", self.args(a))

    def test_welt_anlegen_reicht_ersteller_durch(self):
        code, a = self.post("/worlds/tg/create", "admintok", {"id": "neue"}, wer="bert")
        self.assertEqual(code, 200)
        self.assertIn("--wer bert", self.args(a))
        self.assertNotIn("--owner", self.args(a))

    def test_unsauberer_wer_faellt_zurueck(self):
        _, a = self.post("/snapshot/tg", "admintok", wer="x; rm -rf /")
        self.assertIn("--wer bridge", self.args(a))

    def test_falsches_token_401(self):
        code, _ = self.post("/snapshot/tg", "falsch")
        self.assertEqual(code, 401)


if __name__ == "__main__":
    unittest.main()
