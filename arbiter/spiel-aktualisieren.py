#!/usr/bin/env python3
"""spiel-aktualisieren - bringt einen Spielserver auf den aktuellen Stand, auf Knopfdruck.

Warum es das gibt
=================
Spiel-Clients aktualisieren sich ueber Steam von selbst, Server nicht. Ein Server, der
zurueckliegt, weist aktualisierte Clients ab, steht in der Serverliste aber weiter als
"online" da. Der Platzhalter eines schlafenden Spiels verstaerkt das noch: er haelt es
sichtbar, waehrend dahinter ein Stand liegt, in den niemand mehr hineinkommt.

Gemeldet wurde das laengst (game-version-watcher auf host, taeglich). Gefehlt hat der
Weg vom Melden zum Einspielen: am 2026-09-19 lagen Valheim und Zomboid seit vier Wochen
zurueck, obwohl der Waechter jeden Morgen Bescheid gab. Genau diese Luecke schliesst
dieses Werkzeug, und zwar bewusst als Knopfdruck, nicht als Automatik (Owner-Entscheid
2026-09-19): wann ein Spielstand sich aendert, entscheidet ein Mensch.

Was es tut
==========
  1. fragt, ob gerade jemand spielt. Wenn ja, endet es hier. Es wird nie jemand aus
     einem Spiel geworfen, auch nicht fuer ein Update.
  2. setzt eine Wartung (arbiter --wartung-an). Damit lehnt JEDER Weckweg ab, solange
     die Installation halb alt und halb neu auf der Platte liegt: Discord, Dashboard,
     und vor allem die Greeter von Terraria und Factorio, die ein Beitrittsversuch
     ohne menschliches Zutun ausloest.
  3. faehrt den Server herunter, falls er lief (ueber den Arbiter, damit der Platzhalter
     wieder uebernimmt und die Welt sauber gespeichert wird).
  4. legt einen Welt-Schnappschuss an (arbiter --snapshot, landet im restic-Satz).
  5. spielt das Update ein.
  6. loest die Wartung, startet den Server probeweise und wartet, bis er wirklich
     joinbar ist. Ein Update, das erst beim naechsten Spieler auffaellt, waere kein
     Fortschritt gegenueber dem Zustand vorher.
  7. stellt den Ausgangszustand wieder her: schlief der Server vorher, schlaeft er
     danach wieder.

Die Wartung wird in JEDEM Fall wieder geloest, auch bei Abbruch oder Fehler. Eine
vergessene Wartung waere ein Spiel, das ohne erkennbaren Grund nicht mehr startet;
gegen den Rest deckt die Alarmregel SpielWartungVergessen ab.

Aufruf
======
    spiel-aktualisieren --nur-pruefen            # Stand aller Spiele, aendert nichts
    spiel-aktualisieren valheim                  # Trockenlauf: zeigt den Plan
    spiel-aktualisieren valheim --live           # Server einspielen
    spiel-aktualisieren terraria --mods --live   # Workshop-Mods nachladen
    spiel-aktualisieren --alle --live            # alle mit automatischem Weg
    spiel-aktualisieren valheim --live --ohne-probestart

--mods ist die zweite Arbeit im selben Ablauf: nicht der Server altert, sondern seine
Workshop-Mods. Das betrifft Terraria, weil tModLoader die Mods NICHT beim Serverstart
nachzieht, sondern den lokalen Workshop-Cache nimmt (gemessen 2026-09-19: Recipe Browser
292 Tage zurueck). Project Zomboid braucht es nicht, sein Server prueft die WorkshopItems
bei jedem Start selbst.

Exit: 0 fertig (aktualisiert oder schon aktuell) · 1 Fehler · 3 kein automatischer Weg
      · 4 es wird gerade gespielt
"""
import fcntl, json, os, re, shlex, subprocess, sys, tempfile, time

BASE = os.environ.get("ARBITER_BASE", "/opt/game-arbiter")
ARBITER = os.environ.get("ARBITER_PY", BASE + "/arbiter.py")
GAMES_JSON = BASE + "/games.json"
# Wie lange nach dem Probestart auf "joinbar" gewartet wird. DayZ braucht 2 bis 3 min,
# Zomboid lange genug, dass eine knappe Frist den Erfolg verschweigen wuerde.
PROBE_TIMEOUT_S = int(os.environ.get("PROBE_TIMEOUT_S", "300"))
PROBE_INTERVALL_S = 10


def sagen(*teile):
    print(time.strftime("%H:%M:%S"), *teile, flush=True)


def lauf(cmd, timeout=1800, eingabe=None):
    """Kommando ausfuehren, (rc, ausgabe). Wirft nie."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, input=eingabe)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception as e:
        return 1, "%s: %s" % (type(e).__name__, e)


def arbiter(*args, live=False):
    """Den Arbiter aufrufen. Start, Stopp, Snapshot und Wartung laufen bewusst ueber ihn
    und nicht an ihm vorbei: er haelt den Lock, fuehrt das audit.log und weiss, in welcher
    Reihenfolge Platzhalter und Server den geteilten Port uebernehmen."""
    cmd = [sys.executable, ARBITER] + list(args) + (["--live"] if live else [])
    return lauf(cmd, timeout=600)


def registry():
    with open(GAMES_JSON) as f:
        return (json.load(f) or {}).get("games") or []


def spiel(name):
    for g in registry():
        if g.get("name") == name:
            return g
    return None


def hat_automatischen_weg(g):
    """Laesst sich dieses Spiel ohne menschlichen Zwischenschritt aktualisieren?

    Drei Faelle sagen nein, und alle drei sind echt: DayZ laedt nicht anonym (SteamCMD
    verlangt ein Konto samt Steam-Guard), tModLoader und Paper haben gar keinen Steam-Weg,
    und ein Eintrag ohne update-Block ist schlicht noch nicht hinterlegt. Die Pruefung
    steht hier als eigene Funktion, damit --alle und der Einzelaufruf dieselbe Antwort
    geben: eine zweite, abweichende Auswahl waere genau die Sorte Fehler, die erst bei
    einer Passwortabfrage mitten in der Nacht auffaellt."""
    u = g.get("update") or {}
    if u.get("kind") not in ("steam", "factorio"):
        return False
    return bool(u.get("anonym", True))


def automatische_spiele():
    return [g["name"] for g in registry() if hat_automatischen_weg(g)]


# ── Stand lesen ────────────────────────────────────────────────

def stand_lesen(g):
    """Installierter Stand als Zeichenkette, oder None. Dieselbe Quelle, die der
    game-version-watcher auf host liest: die buildid aus dem Steam-Manifest bzw. die
    Version aus Factorios info.json. Ein eigener zweiter Weg waere eine zweite Wahrheit."""
    u = g.get("update") or {}
    if u.get("kind") == "steam":
        pfad = u.get("manifest") or os.path.join(
            u.get("dir", ""), "steamapps", "appmanifest_%s.acf" % u.get("appid"))
        try:
            with open(pfad) as f:
                m = re.search(r'"buildid"\s*"(\d+)"', f.read())
            return m.group(1) if m else None
        except Exception:
            return None
    if u.get("kind") == "factorio":
        try:
            with open(os.path.join(u.get("dir", ""), "data", "base", "info.json")) as f:
                return (json.load(f) or {}).get("version")
        except Exception:
            return None
    return None


def zustand(name):
    """Was der Arbiter ueber das Spiel weiss (rein lesend, ohne Lock)."""
    rc, out = arbiter("--probe", name)
    try:
        return json.loads(out.strip().splitlines()[-1])
    except Exception:
        return {"game": name, "error": "probe-unlesbar", "rohtext": out[-200:]}


# ── Update-Wege ────────────────────────────────────────────────

def update_steam(u, trocken):
    """SteamCMD als Dienstnutzer. runuser statt su: die Dienstnutzer haben bewusst
    /usr/sbin/nologin als Shell, und 'su - valheim' scheitert daran. runuser fuehrt das
    Kommando direkt aus, ohne Login-Shell."""
    cmd = ["runuser", "-u", u["user"], "--", u["steamcmd"],
           "+force_install_dir", u["dir"], "+login", "anonymous",
           "+app_update", str(u["appid"]), "validate", "+quit"]
    if trocken:
        sagen("   wuerde laufen:", " ".join(shlex.quote(c) for c in cmd))
        return True, "Trockenlauf"
    # Der bekannte Self-Update-Quirk: der erste Lauf nach einem SteamCMD-Update bricht
    # mit "Missing configuration" ab und ist beim zweiten Versuch weg.
    letzte = ""
    for versuch in (1, 2, 3, 4):
        rc, out = lauf(cmd, timeout=3600)
        letzte = out[-1500:]
        if rc == 0 and "Success! App" in out:
            # Die Erfolgszeile suchen statt die letzte Zeile zu nehmen: SteamCMD haengt
            # danach noch Zeilen ueber seinen eigenen Start an, und im Bericht stand dann
            # "Starting /home/.../steamcmd" als Ergebnis eines gelungenen Updates.
            erfolg = [z for z in out.splitlines() if "Success! App" in z]
            return True, erfolg[-1].strip()
        sagen("   SteamCMD-Versuch %d ohne Erfolg (rc=%d), erneut" % (versuch, rc))
        time.sleep(5)
    return False, letzte


def update_factorio(u, trocken):
    """Factorio kommt als tar.xz von factorio.com. Entpackt wird ueber das bestehende
    Verzeichnis: das Archiv bringt bin/ und data/ mit, saves/ und mods/ bleiben liegen."""
    ziel_eltern = os.path.dirname(u["dir"].rstrip("/"))
    if trocken:
        sagen("   wuerde laden:", u["url"], "-> entpacken nach", ziel_eltern)
        return True, "Trockenlauf"
    with tempfile.NamedTemporaryFile(suffix=".tar.xz", delete=False) as fh:
        archiv = fh.name
    try:
        rc, out = lauf(["curl", "-sSL", "--fail", "-o", archiv, u["url"]], timeout=1800)
        if rc != 0:
            return False, "Download fehlgeschlagen: " + out[-300:]
        rc, out = lauf(["tar", "-xJf", archiv, "-C", ziel_eltern], timeout=900)
        if rc != 0:
            return False, "Entpacken fehlgeschlagen: " + out[-300:]
        rc, out = lauf(["chown", "-R", "%s:%s" % (u["user"], u["user"]), u["dir"]], timeout=300)
        if rc != 0:
            return False, "chown fehlgeschlagen: " + out[-300:]
        return True, "entpackt nach " + u["dir"]
    finally:
        try: os.unlink(archiv)
        except OSError: pass


def mod_stand(m):
    """{workshop_id: timeupdated} aus dem acf. Gleiche Quelle wie der Waechter auf host."""
    try:
        with open(m["acf"]) as f:
            text = f.read()
    except Exception:
        return {}
    return {w: int(t) for w, t in
            ((x.group(1), re.search(r'"timeupdated"\s*"(\d+)"', x.group(2)).group(1))
             for x in re.finditer(r'"(\d{6,})"\s*\{(.*?)\n\t\t\}', text, re.S)
             if re.search(r'"timeupdated"\s*"(\d+)"', x.group(2)))}


def update_workshop_mods(m, trocken):
    """Workshop-Mods nachladen, mit demselben Aufruf, den manage-tModLoaderServer.sh nutzt.

    SteamCMD laedt nur, was fehlt oder neuer ist, deshalb geht die ganze Liste hinein und
    nicht nur die als veraltet erkannten: so kann die Liste hier nicht von der wirklichen
    Bestueckung abweichen. Anonym, weil tModLoader ein freies Spiel ist.
    """
    try:
        with open(m["liste"]) as f:
            ids = [z.strip() for z in f if z.strip() and not z.startswith("#")]
    except Exception as e:
        return False, "Mod-Liste %s nicht lesbar: %s" % (m["liste"], e)
    if not ids:
        return False, "Mod-Liste %s ist leer" % m["liste"]
    cmd = ["runuser", "-u", m["user"], "--", m["steamcmd"],
           "+force_install_dir", m["install_dir"], "+login", "anonymous"]
    for w in ids:
        cmd += ["+workshop_download_item", str(m["appid"]), w]
    cmd.append("+quit")
    if trocken:
        sagen("   wuerde laden: %d Mod(s) via %s" % (len(ids), os.path.basename(m["steamcmd"])))
        return True, "Trockenlauf"
    letzte = ""
    for versuch in (1, 2, 3):
        rc, out = lauf(cmd, timeout=3600)
        letzte = out[-1500:]
        erfolge = [z.strip() for z in out.splitlines() if "Success. Downloaded item" in z]
        if rc == 0 and erfolge:
            return True, "%d von %d Mod(s) geladen oder bestaetigt" % (len(erfolge), len(ids))
        sagen("   SteamCMD-Versuch %d ohne Erfolg (rc=%d), erneut" % (versuch, rc))
        time.sleep(5)
    return False, letzte


def nach_update(u, trocken):
    """Nacharbeiten, die das Update selbst zunichte macht. Zomboid ist der Fall, der das
    Feld erzwungen hat: SteamCMD schreibt ProjectZomboid64.json neu und setzt den Heap
    zurueck auf -Xmx8g, womit der gemessene Bedarf ueber dem min_free_mb laege, gegen das
    der Arbiter seine Startzusagen rechnet."""
    for befehl in (u.get("nach_update") or []):
        if trocken:
            sagen("   wuerde nacharbeiten:", befehl)
            continue
        rc, out = lauf(["bash", "-c", befehl], timeout=120)
        sagen("   Nacharbeit %s: %s" % ("ok" if rc == 0 else "FEHLER", befehl))
        if rc != 0:
            return False, out[-300:]
    return True, ""


# ── Ablauf je Spiel ────────────────────────────────────────────

def pruefen(name):
    """Rein lesend: was ist hinterlegt, was ist installiert, laeuft gerade jemand darauf."""
    g = spiel(name)
    u = (g or {}).get("update") or {}
    z = zustand(name)
    spieler = z.get("players")
    print("%-10s %-9s Stand %-12s %s%s" % (
        name, u.get("kind", "FEHLT"), stand_lesen(g) or "?",
        "laeuft" if z.get("service_active") else "schlaeft",
        (", %d Spieler" % spieler) if spieler else ""))
    if u.get("hinweis"):
        print("           %s" % u["hinweis"])


def aktualisieren(name, live, probestart=True, mods=False):
    """mods=False: den SERVER einspielen. mods=True: seine Workshop-Mods nachladen.

    Zwei Arbeiten, ein Ablauf: beide brauchen dieselben Schutzschritte (niemanden stoeren,
    Wartung, herunterfahren, sichern, probeweise starten). Sie getrennt zu bauen haette
    genau diese Schritte dupliziert, und die Kopie waere die, die man beim naechsten Mal
    vergisst nachzuziehen.
    """
    g = spiel(name)
    if not g:
        sagen("unbekanntes Spiel '%s'. Bekannt: %s" % (name, ", ".join(x["name"] for x in registry())))
        return 3
    u = g.get("update") or {}
    m = u.get("mods") or {}
    if mods:
        if m.get("kind") != "workshop":
            sagen("%s: kein Mod-Weg hinterlegt." % name)
            sagen("   Project Zomboid braucht keinen: sein Server prueft die WorkshopItems "
                  "bei jedem Start selbst. Minecraft und DayZ laden Mods nicht ueber den "
                  "Steam-Workshop.")
            return 3
    elif not hat_automatischen_weg(g):
        sagen("%s: kein automatischer Update-Weg." % name)
        if u.get("hinweis"):
            sagen("   " + u["hinweis"])
        return 3

    sperre = open(os.path.join(BASE, "update-%s.lock" % name), "w")
    try:
        fcntl.flock(sperre, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sagen("%s: ein Update laeuft bereits" % name)
        return 1

    z = zustand(name)
    if z.get("error"):
        sagen("%s: Zustand nicht lesbar (%s) -> abgebrochen" % (name, z["error"]))
        return 1
    if (z.get("players") or 0) > 0:
        # Der Kern der Owner-Regel: wer spielt, wird nicht gestoert. Auch nicht kurz.
        sagen("%s: es spielen gerade %d Person(en). Nichts angefasst, spaeter erneut versuchen."
              % (name, z["players"]))
        return 4
    lief_vorher = bool(z.get("service_active"))
    vorher = mod_stand(m) if mods else stand_lesen(g)
    sagen("%s: %s, Server %s%s" % (
        name,
        ("%d Workshop-Mod(s) installiert" % len(vorher)) if mods else ("Stand %s" % (vorher or "unbekannt")),
        "laeuft" if lief_vorher else "schlaeft",
        "" if live else "  [TROCKENLAUF, --live fehlt]"))

    if not live:
        sagen("   wuerde: Wartung setzen, %sSnapshot anlegen, %s einspielen, "
              "probeweise starten, %s" % ("Server stoppen, " if lief_vorher else "",
                                          "Mods" if mods else "Update",
                                          "wieder schlafen legen" if not lief_vorher else "laufen lassen"))
        if mods:
            update_workshop_mods(m, True)
        elif u["kind"] == "steam":
            update_steam(u, True)
        else:
            update_factorio(u, True)
        nach_update(u, True)
        return 0

    wartung_gesetzt = False
    try:
        rc, out = arbiter("--wartung-an", name, "--grund",
                          "%s laeuft (spiel-aktualisieren)" % ("Mod-Update" if mods else "Update"))
        if rc != 0:
            sagen("%s: Wartung liess sich nicht setzen -> abgebrochen (%s)" % (name, out[-200:]))
            return 1
        wartung_gesetzt = True
        sagen("   Wartung gesetzt: Weckversuche werden ab jetzt abgelehnt")

        if lief_vorher:
            sagen("   Server herunterfahren (Welt speichern, Platzhalter uebernimmt)")
            arbiter("--sleep", name, live=True)
            time.sleep(5)

        sagen("   Welt-Schnappschuss anlegen")
        rc, out = arbiter("--snapshot", name, live=True)
        if rc != 0:
            # Kein Abbruch: die Welt liegt zusaetzlich im naechtlichen Schnappschuss und im
            # restic-Satz. Aber es gehoert in den Bericht, damit niemand glaubt, es haenge
            # ein frisches Netz darunter.
            sagen("   WARNUNG: Snapshot meldet rc=%d. Naechtlicher Stand und restic bleiben, "
                  "ein FRISCHER Rueckweg fehlt aber. (%s)" % (rc, out.strip()[-200:]))

        sagen("   %s einspielen (%s)" % ("Mods" if mods else "Update",
                                         m.get("kind") if mods else u["kind"]))
        if mods:
            ok, meldung = update_workshop_mods(m, False)
        elif u["kind"] == "steam":
            ok, meldung = update_steam(u, False)
        else:
            ok, meldung = update_factorio(u, False)
        if not ok:
            sagen("   FEHLER beim Update: %s" % meldung[-500:])
            return 1
        sagen("   %s" % meldung)

        ok, meldung = nach_update(u, False)
        if not ok:
            sagen("   FEHLER in der Nacharbeit: %s" % meldung)
            return 1

        if mods:
            nachher = mod_stand(m)
            geaendert = [w for w, t in nachher.items() if vorher.get(w) != t]
            if geaendert:
                for w in geaendert:
                    sagen("   Mod %s: %s -> %s" % (
                        w, time.strftime("%Y-%m-%d", time.localtime(vorher.get(w, 0))),
                        time.strftime("%Y-%m-%d", time.localtime(nachher[w]))))
            else:
                sagen("   Kein Mod-Stand veraendert: alle waren bereits aktuell")
        else:
            nachher = stand_lesen(g)
            if vorher and nachher and vorher == nachher:
                sagen("   Stand unveraendert (%s): war bereits aktuell" % nachher)
            else:
                sagen("   Stand %s -> %s" % (vorher or "?", nachher or "?"))
    finally:
        if wartung_gesetzt:
            arbiter("--wartung-aus", name)
            sagen("   Wartung geloest")
        try:
            fcntl.flock(sperre, fcntl.LOCK_UN)
        except Exception:
            pass

    if not probestart:
        sagen("%s: fertig (Probestart uebersprungen)" % name)
        return 0

    sagen("   Probestart: der Server muss wirklich joinbar werden")
    rc, out = arbiter("--wake", name, live=True)
    if rc != 0:
        letzte = (out.strip().splitlines() or ["ohne Ausgabe"])[-1]
        sagen("   FEHLER: Start abgelehnt (%s)" % letzte[:200])
        return 1
    begonnen = time.time()
    erreichbar = False
    while time.time() - begonnen < PROBE_TIMEOUT_S:
        time.sleep(PROBE_INTERVALL_S)
        z = zustand(name)
        if z.get("reachable"):
            erreichbar = True
            break
        if not z.get("service_active"):
            sagen("   Unit ist wieder aus -> Start hat nicht gehalten")
            break
    if erreichbar:
        sagen("   joinbar nach %ds" % int(time.time() - begonnen))
    else:
        sagen("   NICHT joinbar innerhalb von %ds. Ursache lesen: journalctl -u %s -n 50"
              % (PROBE_TIMEOUT_S, g.get("service") or name))

    if not lief_vorher:
        sagen("   wieder schlafen legen (Ausgangszustand)")
        arbiter("--sleep", name, live=True)

    sagen("%s: %s" % (name, "fertig" if erreichbar else "FERTIG, ABER OHNE BELEG: der Server "
                      "kam im Probestart nicht hoch"))
    return 0 if erreichbar else 1


def main():
    args = [a for a in sys.argv[1:]]
    live = "--live" in args
    probestart = "--ohne-probestart" not in args
    mods = "--mods" in args
    namen = [a for a in args if not a.startswith("-")]

    if os.geteuid() != 0:
        print("Das Werkzeug braucht root (systemctl, runuser, Schreibrechte in den "
              "Spiel-Verzeichnissen).", file=sys.stderr)
        return 1
    if not os.path.isfile(GAMES_JSON):
        print("Registry %s fehlt: laeuft dieses Werkzeug auf dem richtigen Wirt?" % GAMES_JSON,
              file=sys.stderr)
        return 1

    if "--nur-pruefen" in args:
        for g in registry():
            pruefen(g["name"])
        return 0

    if "--alle" in args:
        namen = ([g["name"] for g in registry()
                  if ((g.get("update") or {}).get("mods") or {}).get("kind") == "workshop"]
                 if mods else automatische_spiele())
        sagen("alle Spiele mit %s: %s" % ("Mod-Weg" if mods else "automatischem Weg",
                                          ", ".join(namen) or "(keine)"))
    if not namen:
        print("Kein Spiel genannt. Beispiele:\n"
              "  spiel-aktualisieren --nur-pruefen        Stand aller Spiele, aendert nichts\n"
              "  spiel-aktualisieren valheim              Trockenlauf mit Plan\n"
              "  spiel-aktualisieren valheim --live       einspielen\n"
              "  spiel-aktualisieren --alle --live        alle mit automatischem Weg",
              file=sys.stderr)
        return 1

    schlechtester = 0
    for n in namen:
        rc = aktualisieren(n, live, probestart, mods=mods)
        schlechtester = max(schlechtester, rc)
    return schlechtester


if __name__ == "__main__":
    sys.exit(main())
