#!/usr/bin/env python3
"""Prueft die beiden Schutzmechanismen im Arbiter-Snapshotpfad selbst:
   - _snap_tar bricht bei rc=9 (Welt-Pruefung) ab und laesst den alten Stand liegen
   - _shrink_ok verhindert, dass ein stark geschrumpftes Archiv ein gutes ueberschreibt
Beide entscheiden darueber, ob ein kaputter Weltstand den letzten guten verdraengt.
Laeuft ohne LXC: run()/audit() werden ersetzt."""
import importlib.util, os, shutil, sys, tempfile

spec = importlib.util.spec_from_file_location("arb", sys.argv[1])
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

LOG = []
m.audit = lambda msg: LOG.append(msg)
m._lxc_running = lambda ctid: True
# Der Ort, an dem die Welt liegt: hier ein LXC-Spiel wie terraria auf .18.
TERRARIA_LXC = {"name": "terraria", "kind": "lxc-systemd", "ctid": 205}

fails = []
def t(name, cond, detail=""):
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (("  " + detail) if detail else ""))
    if not cond:
        fails.append(name)

tmpd = tempfile.mkdtemp()
dest = os.path.join(tmpd, "welt.tar.gz")

def write(path, nbytes):
    with open(path, "wb") as f:
        f.write(b"x" * nbytes)

print("1. Welt-Pruefung schlaegt fehl (rc=9) -> alter Stand bleibt unangetastet")
write(dest, 14_000_000)
before = open(dest, "rb").read()
def fake_run(cmd, timeout=30):
    # so verhaelt sich der echte Aufruf: das Pruef-Fragment beendet mit 9,
    # der Tar-Strom bleibt leer -> die .tmp-Datei entsteht trotzdem (Redirect).
    out = cmd.split(" > ")[-1].strip()
    open(out, "wb").close()
    return 9, "", "zu klein: Worlds/Greenleaf.wld (0 < 4000000 Byte)"
m.run = fake_run
LOG.clear()
ok = m._snap_tar(TERRARIA_LXC, "/home/terraria", ["Worlds"], dest, check="pruef", shrink_max_pct=50)
t("meldet Misserfolg", ok is False)
t("alter Stand unveraendert", open(dest, "rb").read() == before)
t("keine .tmp-Leiche", not os.path.exists(dest + ".tmp"))
t("Meldung nennt den Grund", any("Welt-Pruefung fehlgeschlagen" in x and "zu klein" in x for x in LOG),
  "-> " + (LOG[0][:80] if LOG else "keine"))

print("2. Archiv schrumpft stark (Welt fehlt im Tar) -> Uebernahme verweigert")
write(dest, 14_000_000)
before = open(dest, "rb").read()
def fake_run_small(cmd, timeout=30):
    out = cmd.split(" > ")[-1].strip()
    write(out, 200_000)          # 200 KB gegen 14 MB = 98,6 % geschrumpft
    return 0, "", ""
m.run = fake_run_small
LOG.clear()
ok = m._snap_tar(TERRARIA_LXC, "/home/terraria", ["Worlds"], dest, shrink_max_pct=50)
t("meldet Misserfolg", ok is False)
t("alter Stand unveraendert", open(dest, "rb").read() == before)
t("Meldung nennt Groessen + Ausweg", any("geschrumpft" in x and "loeschen" in x for x in LOG),
  "-> " + (LOG[0][:90] if LOG else "keine"))

print("3. normales Wachstum (Mods kommen dazu) -> wird uebernommen")
write(dest, 14_000_000)
def fake_run_big(cmd, timeout=30):
    out = cmd.split(" > ")[-1].strip()
    write(out, 117_000_000)
    return 0, "", ""
m.run = fake_run_big
ok = m._snap_tar(TERRARIA_LXC, "/home/terraria", ["Worlds"], dest, shrink_max_pct=50)
t("uebernommen", ok is True)
t("neue Groesse aktiv", os.path.getsize(dest) == 117_000_000)

print("4. leichtes Schrumpfen (normale gzip-Schwankung) -> wird uebernommen")
write(dest, 14_000_000)
def fake_run_slightly_smaller(cmd, timeout=30):
    out = cmd.split(" > ")[-1].strip()
    write(out, 13_000_000)       # -7 %
    return 0, "", ""
m.run = fake_run_slightly_smaller
ok = m._snap_tar(TERRARIA_LXC, "/home/terraria", ["Worlds"], dest, shrink_max_pct=50)
t("uebernommen", ok is True)

print("5. erster Lauf einer neuen Welt (kein Vorgaenger) -> kein Fehlalarm")
os.remove(dest)
m.run = fake_run_small
ok = m._snap_tar(TERRARIA_LXC, "/home/terraria", ["Worlds"], dest, shrink_max_pct=50)
t("uebernommen", ok is True)

print("6. manueller Snapshot (shrink_max_pct=None) -> Schutz greift nicht")
write(dest, 14_000_000)
m.run = fake_run_small
ok = m._snap_tar(TERRARIA_LXC, "/home/terraria", ["Worlds"], dest, shrink_max_pct=None)
t("uebernommen", ok is True, "(Timestamp-Namen verdraengen nichts)")

print("7. tar rc=1 ('file changed as we read it') bleibt toleriert")
write(dest, 14_000_000)
def fake_run_rc1(cmd, timeout=30):
    out = cmd.split(" > ")[-1].strip()
    write(out, 14_100_000)
    return 1, "", "tar: file changed as we read it"
m.run = fake_run_rc1
ok = m._snap_tar(TERRARIA_LXC, "/home/terraria", ["Worlds"], dest, shrink_max_pct=50)
t("uebernommen", ok is True, "(Server schreibt live weiter, kein Fehler)")

shutil.rmtree(tmpd, ignore_errors=True)
print()
print("ERGEBNIS: %s" % ("alle Faelle wie erwartet" if not fails else "%d abweichend: %s" % (len(fails), fails)))
sys.exit(1 if fails else 0)
