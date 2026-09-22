#!/bin/bash
# arbiter-tests.sh, Szenario-Suite fuer mc-orchestrator (Node .18, LXC 203, Gate-Architektur).
# MC-Lifecycle laeuft LIVE (jetzt sicher: mc_start/mc_stop treffen NUR mc-poc, Gate bleibt oben).
# Lab-/Verdraengungs-Logik bleibt dry/confirm-gated. Gate + Win-VMs werden nicht angefasst.
set -u
# ARB_PY zeigt per Vorgabe auf den ausgerollten Arbiter. Im Repo:
#   ARB_PY=arbiter/arbiter.py tests/arbiter-tests.sh   -> die gestubbten Bloecke T6-T10 laufen ohne Node.
ARB_PY="${ARB_PY:-/opt/game-arbiter/arbiter.py}"; export ARB_PY
ARB="python3 $ARB_PY"
CTID=203; CT=mc-poc; GATE=mc-velocity   # seit dem Velocity-Umbau 2026-08-10 heisst das Gate so (vorher mc-gate)
PASS=0; FAIL=0
ok(){ echo "  [PASS] $1"; PASS=$((PASS+1)); }
no(){ echo "  [FAIL] $1"; FAIL=$((FAIL+1)); }
ctstate(){ pct exec $CTID -- docker inspect -f '{{.State.Status}}' $CT 2>/dev/null || echo absent; }
gatestate(){ pct exec $CTID -- docker inspect -f '{{.State.Status}}' $GATE 2>/dev/null || echo absent; }
health(){ pct exec $CTID -- docker inspect -f '{{.State.Health.Status}}' $CT 2>/dev/null || echo absent; }
mode(){ python3 -c "import json;print(json.load(open('/opt/game-arbiter/state.json'))['mode'])" 2>/dev/null; }
rc_(){ python3 -c "import json;print(json.load(open('/opt/game-arbiter/state.json'))['restart_count'])" 2>/dev/null; }
wait_health(){ for i in $(seq 1 30); do [ "$(health)" = healthy ] && return 0; sleep 4; done; return 1; }

MC_DA=$(python3 -c "
import json,os
p=os.path.dirname(os.environ['ARB_PY'])+'/arbiter.json'
try: print('1' if json.load(open(p)).get('roles',{}).get('minecraft',True) else '0')
except Exception: print('1')")

echo "############ game-arbiter Szenario-Suite ($(date +%H:%M:%S)) ############"
# Die LIVE-Bloecke fahren echte Start/Stop-Zyklen auf Node .18 und brauchen dort pct + Minecraft.
# Ohne beides werden sie NICHT ausgefuehrt -- und das wird gesagt, statt sie stillschweigend
# als bestanden zu zaehlen.
LIVE_MOEGLICH=0
command -v pct >/dev/null 2>&1 && [ "$MC_DA" = "1" ] && LIVE_MOEGLICH=1
if [ "$LIVE_MOEGLICH" != "1" ]; then
  echo "  HINWEIS: kein pct und/oder kein Minecraft an diesem Ort -> die LIVE-Bloecke T1-T5 entfallen."
  echo "           Das ist kein PASS: sie wurden nicht geprueft, sondern nicht ausgefuehrt."
fi
if [ "$LIVE_MOEGLICH" = "1" ]; then
$ARB --release >/dev/null 2>&1; $ARB --reset >/dev/null 2>&1
$ARB --live --start-mc >/dev/null 2>&1; $ARB --live >/dev/null 2>&1; wait_health >/dev/null 2>&1

echo; echo "=== T1: MC-Stop/Start trifft NUR mc-poc, Gate bleibt oben (--stop-mc/--start-mc = sleep/wake) ==="
G0=$(gatestate)
$ARB --live --stop-mc >/dev/null; $ARB --live >/dev/null; sleep 8
echo "  nach stop-mc: mc=$(ctstate) gate=$(gatestate)"
{ [ "$(ctstate)" != running ] && [ "$(gatestate)" = running ]; } && ok "MC gestoppt, Gate bleibt up" || no "Gate mitgerissen oder MC nicht gestoppt"
$ARB --live --start-mc >/dev/null; $ARB --live >/dev/null
if wait_health; then
  $ARB --live >/dev/null   # STARTING -> ACTIVE (ein Tick nachdem healthy erreicht ist)
  echo "  nach start-mc: mc=$(ctstate)/$(health) gate=$(gatestate) mode=$(mode)"
  { [ "$(mode)" = MINECRAFT_ACTIVE ] && [ "$(gatestate)" = running ]; } && ok "MC wieder healthy, Gate durchgehend up" || no "MC/Gate-Zustand falsch"
else no "MC healthy nach Neustart nicht erreicht"; fi

echo; echo "=== T2: Crash-Recovery (Selbstheilung, Gate unberuehrt) ==="
$ARB --live >/dev/null
echo "  Crash: docker kill $CT"; pct exec $CTID -- docker kill $CT >/dev/null 2>&1; sleep 2
$ARB --live >/dev/null   # Crash erkannt -> Restart
if wait_health; then
  $ARB --live >/dev/null
  echo "  geheilt: mode=$(mode) rc=$(rc_) gate=$(gatestate)"
  { [ "$(mode)" = MINECRAFT_ACTIVE ] && [ "$(rc_)" = 0 ] && [ "$(gatestate)" = running ]; } && ok "Crash geheilt, Zaehler 0, Gate up" || no "Recovery unvollstaendig"
else no "Recovery healthy nicht erreicht"; fi

echo; echo "=== T3: FAILED-Latch (Anti-Restart-Sturm; MC reserviert = Crash-Maschine aktiv) ==="
python3 -c "import json;p='/opt/game-arbiter/state.json';s=json.load(open(p));s['restart_count']=3;s['reservation']='minecraft';s['mode']='MINECRAFT_ACTIVE';json.dump(s,open(p,'w'))"
pct exec $CTID -- docker kill $CT >/dev/null 2>&1; sleep 2
$ARB --live >/dev/null   # 4 > max 3 -> FAILED
echo "  mode=$(mode)"
[ "$(mode)" = FAILED ] && ok "FAILED-Latch ausgeloest" || no "kein FAILED"
$ARB --live >/dev/null; sleep 2
[ "$(ctstate)" != running ] && ok "kein Auto-Restart im FAILED-Zustand" || no "Restart trotz FAILED"
$ARB --reset >/dev/null; [ "$(mode)" = IDLE ] && ok "--reset -> IDLE" || no "Reset fehlgeschlagen"

echo; echo "=== T4: Yield-Politik (dry), MC ist on-demand, laeuft NUR wenn reserviert ==="
$ARB --release >/dev/null; $ARB --reset >/dev/null
# 4a: nicht reserviert (Node idle) -> MC weicht (kein immer-online mehr)
$ARB 2>&1 | grep -qi "nicht reserviert.*weicht" && ok "4a res=none -> MC weicht (on-demand, kein immer-online)" || no "4a"
# 4b: Lab reserviert -> MC weicht ebenfalls
$ARB --reserve-lab >/dev/null
$ARB 2>&1 | grep -qi "nicht reserviert.*weicht" && ok "4b Lab reserviert -> MC weicht" || no "4b"
$ARB --release >/dev/null; $ARB --reset >/dev/null
# 4c: minecraft reserviert -> MC on-demand aktiv (gleichrangig zu den Games)
$ARB --reserve-game minecraft >/dev/null
$ARB 2>&1 | grep -qi "minecraft reserviert -> MC on-demand aktiv" && ok "4c minecraft reserviert -> MC aktiv" || no "4c"
$ARB --release >/dev/null; $ARB --reset >/dev/null

echo; echo "=== T5: Lab-Kommandos (dry) ==="
$ARB --start-lab 2>&1 | grep -qiE "reserv|schlafen|gestartet|ABGELEHNT" && ok "5a --start-lab-Flow laeuft (dry)" || no "5a"
$ARB --stop-lab 2>&1 | grep -qi "beendet" && ok "5b --stop-lab (dry)" || no "5b"
$ARB --evict-lab 2>&1 | grep -qiE "kein Win-Lab|--confirm-evict" && ok "5c --evict-lab ohne Lab/confirm sauber" || no "5c"
$ARB --simulate-lab --evict-lab --confirm-evict 2>&1 | grep -qi "qm shutdown" && ok "5d --evict-lab --confirm-evict -> wuerde verdraengen (dry)" || no "5d"
$ARB --release >/dev/null; $ARB --reset >/dev/null

fi   # Ende der MC-/Lab-Live-Bloecke T1-T5

echo; echo "=== T6: Game-Logik generisch (dayz, isoliert, state-sicher, LIVE-safe) ==="
python3 - <<'PYEOF'
import importlib.machinery, time
m = importlib.machinery.SourceFileLoader("arb", __import__("os").environ["ARB_PY"]).load_module()
m.DRY = True; m.save_state = lambda s: None; m.emit_state = lambda *a, **k: None
# Sensoren stubben -> reine Logik, kein echter pct/qm/A2S, kein State-Write
m.mc_lxc_running=lambda:True; m.mc_ct_state=lambda:"exited"; m.mc_health=lambda:"n/a"
m.mc_exit_code=lambda:0; m.gate_running=lambda:True; m.ensure_gate=lambda:None
m.lab_running=lambda:[]; m.free_mb=lambda:5000
# GAMES ueberschreiben: dayz mit idle_timeout>0 (auto-off testbar, unabhaengig von games.json)
m.GAMES=[{"name":"dayz","kind":"lxc-systemd","ctid":204,"service":"dayz-server",
          "probe":{"type":"a2s","ip":"1.2.3.4","port":27016},"min_free_mb":4200,"idle_timeout_s":300}]
P=F=0
def chk(c,l):
    global P,F
    print(("  [PASS] " if c else "  [FAIL] ")+l); P+=c; F+=(not c)
def slot(**kw): return dict({"since":None,"idle_since":None,"unused_since":None,"was_used":False,"world":None},**kw)
base=lambda **kw: dict({"mode":"IDLE","reservation":"none","games":{"dayz":slot()},"restart_count":0,
  "idle_since":None,"lab_since":None},**kw)
# 6a dayz reserviert, Game laeuft, leer+ungenutzt -> MC weicht, Game bleibt reserviert
m.game_active=lambda g:True; m.game_players=lambda g:0
s=m.tick(base(),False,False); chk("dayz" in s["games"] and s["mode"]=="IDLE","6a Spiel reserviert -> MC weicht, Spiel bleibt")
# 6b genutzt + jetzt leer > timeout -> auto-off (Reservierung faellt weg)
s=m.tick(base(games={"dayz":slot(idle_since=time.time()-9999,was_used=True)}),False,False)
chk("dayz" not in s["games"],"6b leer nach Nutzung > timeout -> auto-off")
# 6c LEERES Game ohne Reservierung -> Aufraeum-Pfad
stopped={"v":False}; m.game_stop=lambda g:stopped.update(v=True)
s=m.tick(base(games={}),False,False); chk(stopped["v"] and not s["games"],"6c leeres Spiel ohne Reservierung -> aufraeumen")
# 6d SICHERHEITSGURT: BESETZTES Game ohne Reservierung -> uebernehmen statt stoppen
stopped={"v":False}; m.game_players=lambda g:2
s=m.tick(base(games={}),False,False)
chk(not stopped["v"] and "dayz" in s["games"],"6d besetztes Spiel ohne Reservierung -> uebernommen, NICHT gestoppt")
m.game_players=lambda g:0
# 6e reserviert + Server aus -> Start ausgeloest
m.game_active=lambda g:False; started={"v":False}; m.game_start=lambda g,world=None:started.update(v=True)
m.tick(base(),False,False); chk(started["v"],"6e reserviert + Server aus -> Start")
print("  T6: %d PASS / %d FAIL"%(P,F)); import sys; sys.exit(1 if F else 0)
PYEOF
[ $? -eq 0 ] && ok "T6 Game-Logik komplett gruen" || no "T6 Game-Logik"

echo; echo "=== T7: Multi-Game, nur reserviertes laeuft, andere weichen + Registry/CLI ==="
python3 - <<'PYEOF'
import importlib.machinery
m = importlib.machinery.SourceFileLoader("arb", __import__("os").environ["ARB_PY"]).load_module()
m.PROFILE={"node":"test","roles":{"minecraft":True,"lab":True}}  # Rollen der Tests, nicht die des Wirts
m.DRY=True; m.save_state=lambda s:None; m.emit_state=lambda *a,**k:None
m.mc_lxc_running=lambda:True; m.mc_ct_state=lambda:"exited"; m.mc_health=lambda:"n/a"
m.mc_exit_code=lambda:0; m.gate_running=lambda:True; m.ensure_gate=lambda:None
m.lab_running=lambda:[]; m.free_mb=lambda:8000
m.GAMES=[{"name":"dayz","kind":"lxc-systemd","ctid":204,"service":"dayz-server","probe":{"type":"a2s","ip":"1.1.1.1","port":1},"min_free_mb":4200,"idle_timeout_s":900},
         {"name":"valheim","kind":"lxc-docker","ctid":205,"container":"valheim","probe":{"type":"a2s","ip":"1.1.1.2","port":2},"min_free_mb":3500,"idle_timeout_s":900}]
P=F=0
def chk(c,l):
    global P,F
    print(("  [PASS] " if c else "  [FAIL] ")+l); P+=c; F+=(not c)
active={"dayz":True,"valheim":True}
m.game_active=lambda g: active.get(g["name"],False)
m.game_players=lambda g:0; m.game_start=lambda g,world=None:None
stopped=[]; m.game_stop=lambda g: stopped.append(g["name"])
base=lambda **kw: dict({"mode":"IDLE","reservation":"none","restart_count":0,
  "idle_since":None,"lab_since":None,
  "games":{"dayz":{"since":None,"idle_since":None,"unused_since":None,"was_used":False,"world":None}}},**kw)
m.tick(base(),False,False)
chk("valheim" in stopped and "dayz" not in stopped,"7a dayz reserviert -> leeres valheim weicht, dayz bleibt")
# 7a2/7a3 Auto-Off gegen den Schutz. Ein leeres Spiel, das die Frist ueberschritten hat, geht
# normalerweise aus (7a2). Ist es reserviert, bleibt es an (7a3), sonst waere der Schalter
# wirkungslos, denn 'reserviert' und 'gerade leer' treffen typischerweise zusammen.
import time as _t
_alt = lambda **kw: dict({"since":None,"idle_since":_t.time()-99999,"unused_since":None,
                          "was_used":True,"world":None},**kw)
active={"dayz":True,"valheim":False}
stopped.clear(); m.tick(base(games={"dayz":_alt()}),False,False)
chk("dayz" in stopped,"7a2 leer ueber der Frist -> auto-off")
stopped.clear(); s7=base(games={"dayz":_alt(geschuetzt=True)}); m.tick(s7,False,False)
chk("dayz" not in stopped,"7a3 reserviert + leer ueber der Frist -> bleibt an")
chk(s7["games"]["dayz"]["idle_since"] is None,"7a4 Schutz setzt die Idle-Uhr zurueck (kein Fallbeil beim Freigeben)")
active={"dayz":True,"valheim":True}; stopped.clear()
chk(m.game_by_name("valheim") is not None and m.game_names()==["dayz","valheim"],"7b Registry-Lookup + game_names")
chk(m.game_by_name("nope") is None,"7c unbekanntes Game -> None")
print("  T7: %d PASS / %d FAIL"%(P,F)); import sys; sys.exit(1 if F else 0)
PYEOF
[ $? -eq 0 ] && ok "T7 Multi-Game gruen" || no "T7 Multi-Game"

echo; echo "=== T8: Lab Idle-Auto-Off + Gaming-Vorrang beim Lab-Start (isoliert, gestubbt) ==="
python3 - <<'PYEOF'
import importlib.machinery, time
m = importlib.machinery.SourceFileLoader("arb", __import__("os").environ["ARB_PY"]).load_module()
m.PROFILE={"node":"test","roles":{"minecraft":True,"lab":True}}  # Rollen der Tests, nicht die des Wirts
m.DRY=True; m.save_state=lambda s:None; m.emit_state=lambda *a,**k:None; m.audit=lambda msg:None
m.mc_lxc_running=lambda:True; m.gate_running=lambda:True; m.ensure_gate=lambda:None
m.free_mb=lambda:8000; m.lab_ram_need=lambda:4096; m.mc_exit_code=lambda:0
m.CFG["lab_idle_timeout_s"]=2700
P=F=0
def chk(c,l):
    global P,F
    print(("  [PASS] " if c else "  [FAIL] ")+l); P+=c; F+=(not c)
base=lambda **kw: dict({"mode":"IDLE","reservation":"none","games":{},"restart_count":0,
  "idle_since":None,"lab_since":None,"lab_idle_since":None},**kw)
m.GAMES=[]; m.mc_ct_state=lambda:"exited"; m.mc_health=lambda:"n/a"; m.mc_players=lambda:0
# 8a unreserviert + keine Sitzung + idle > timeout -> Auto-Off
m.lab_running=lambda:[210]; m.lab_session_active=lambda:False
stopped={"v":False}; m.stop_lab_graceful=lambda lab:(stopped.update(v=True) or True)
s=m.tick(base(reservation="none",lab_since=time.time()-9e4,lab_idle_since=time.time()-9999),False,False)
chk(stopped["v"] and s.get("lab_since") is None,"8a unreserviert + keine Sitzung + idle>timeout -> Auto-Off")
# 8b aktive Sitzung -> KEIN Auto-Off, Idle-Timer reset
m.lab_session_active=lambda:True; st2={"v":False}; m.stop_lab_graceful=lambda lab:(st2.update(v=True) or True)
s=m.tick(base(reservation="none",lab_since=time.time()-9e4,lab_idle_since=time.time()-9999),False,False)
chk(not st2["v"] and s.get("lab_idle_since") is None,"8b aktive Sitzung -> kein Auto-Off, Idle-Timer reset")
# 8c RESERVIERTES Lab + keine Sitzung -> geschuetzt
m.lab_session_active=lambda:False; st3={"v":False}; m.stop_lab_graceful=lambda lab:(st3.update(v=True) or True)
s=m.tick(base(reservation="lab",lab_since=time.time()-9e4,lab_idle_since=time.time()-9999),False,False)
chk(not st3["v"],"8c reserviertes Lab -> Idle-Auto-Off geschuetzt")
# --- Lab-Start-Vorrang (blocking_role: nur belegte Rolle blockiert) ---
m.lab_running=lambda:[]
m.GAMES=[{"name":"dayz","kind":"lxc-systemd","ctid":204,"service":"x","probe":{"type":"a2s","ip":"1","port":1},"min_free_mb":100,"idle_timeout_s":900}]
cmds={"v":[]}; m.act=lambda desc,cmd,**k:(cmds["v"].append(cmd) or True)
# 8d Lab sanft + Game MIT Spielern -> ABGELEHNT
cmds["v"]=[]; m.game_active=lambda g:True; m.game_players=lambda g:2
s=base(games={"dayz":{"since":None,"idle_since":None,"unused_since":None,"was_used":True,"world":None}}); m.cmd_start_lab(s,False)
chk(not any("qm start" in c for c in cmds["v"]),"8d Lab sanft + Game mit Spielern -> ABGELEHNT")
# 8e Lab sanft + Game LEER (0 Spieler) -> Lab startet unreserviert
cmds["v"]=[]; m.game_players=lambda g:0
s=base(reservation="none"); m.cmd_start_lab(s,False)
chk(any("qm start 210" in c for c in cmds["v"]) and s["reservation"]=="none","8e Lab sanft + leeres Game -> startet unreserviert")
# 8f Lab --reserve + Game MIT Spielern -> ABGELEHNT. Bis 2026-08-22 war --reserve der
# Erzwingen-Modus und beendete die laufende Partie; seit dem 2026-08-23 ist der Wartungsmodus
# nur noch ein Schutz fuer die Zukunft, kein Freibrief gegen laufenden Betrieb.
cmds["v"]=[]; m.game_active=lambda g:True; m.game_players=lambda g:2; gst={"v":False}; m.game_stop=lambda g:gst.update(v=True)
s=base(games={"dayz":{"since":None,"idle_since":None,"unused_since":None,"was_used":True,"world":None}}); m.cmd_start_lab(s,True)
chk(not gst["v"] and not any("qm start" in c for c in cmds["v"]),"8f Lab --reserve + bespieltes Game -> ABGELEHNT, Game bleibt")
# 8g Lab --reserve + LEERES Game -> Game weicht, Lab startet reserviert (Wartungsmodus)
cmds["v"]=[]; m.game_players=lambda g:0; gst={"v":False}
s=base(games={"dayz":{"since":None,"idle_since":None,"unused_since":None,"was_used":True,"world":None}}); m.cmd_start_lab(s,True)
chk(gst["v"] and s["reservation"]=="lab" and any("qm start 210" in c for c in cmds["v"]),"8g Lab --reserve + leeres Game -> Game weicht, Lab reserviert")
print("  T8: %d PASS / %d FAIL"%(P,F)); import sys; sys.exit(1 if F else 0)
PYEOF
[ $? -eq 0 ] && ok "T8 Lab Idle+Vorrang gruen" || no "T8 Lab"

echo; echo "=== T9: Mehrere Spiele gleichzeitig (Platz entscheidet, nicht Exklusivitaet) ==="
python3 - <<'PYEOF'
import importlib.machinery
m = importlib.machinery.SourceFileLoader("arb", __import__("os").environ["ARB_PY"]).load_module()
m.PROFILE={"node":"test","roles":{"minecraft":True,"lab":True}}  # Rollen der Tests, nicht die des Wirts
m.DRY=True; m.save_state=lambda s:None; m.emit_state=lambda *a,**k:None; m.audit=lambda msg:None
m.mc_lxc_running=lambda:True; m.mc_ct_state=lambda:"exited"; m.mc_health=lambda:"n/a"; m.mc_players=lambda:0
m.lab_running=lambda:[]; m.free_mb=lambda:8000; m.act=lambda *a,**k:True; m.game_start=lambda g,world=None:None
m.GAMES=[{"name":"dayz","kind":"lxc-systemd","ctid":204,"service":"x","probe":{"type":"a2s","ip":"1","port":1},"min_free_mb":100,"idle_timeout_s":900},
         {"name":"valheim","kind":"lxc-docker","ctid":205,"container":"v","probe":{"type":"tcp","ip":"1","port":2},"min_free_mb":100,"idle_timeout_s":900}]
P=F=0
def chk(c,l):
    global P,F
    print(("  [PASS] " if c else "  [FAIL] ")+l); P+=c; F+=(not c)
def slot(**kw): return dict({"since":None,"idle_since":None,"unused_since":None,"was_used":False,"world":None},**kw)
base=lambda **kw: dict({"reservation":"none","games":{},"lab_since":None,"lab_idle_since":None},**kw)
# 9a genug RAM: valheim startet NEBEN dem bespielten dayz -- niemand wird herausgeworfen.
# Bis 2026-08-22 war genau das abgelehnt worden, weil nur ein Spiel laufen durfte.
m.game_active=lambda g:g["name"]=="dayz"; m.game_players=lambda g:3 if g["name"]=="dayz" else 0
gstop=[]; m.game_stop=lambda g:gstop.append(g["name"])
s9=base(games={"dayz":slot(was_used=True)})
chk(m.cmd_start_game(s9,"valheim")=="started" and not gstop,"9a Platz da -> valheim startet neben dayz(3P), niemand weicht")
chk(sorted(s9["games"])==["dayz","valheim"],"9a2 beide Spiele sind reserviert")
# 9b RAM knapp + dayz LEER -> das leere weicht
m.free_mb=lambda:3000; m.game_players=lambda g:0
m.GAMES[1]["min_free_mb"]=4000; m.GAMES[0]["min_free_mb"]=100; m.GAMES[0]["ram_mb"]=4000
gstop=[]
chk(m.cmd_start_game(base(games={"dayz":slot()}),"valheim")=="started" and "dayz" in gstop,"9b RAM knapp + dayz leer -> valheim started, dayz weicht")
# 9c RAM knapp + dayz BESETZT -> lieber gar nicht starten als jemanden herauswerfen
m.game_players=lambda g:3 if g["name"]=="dayz" else 0
gstop=[]
chk(m.cmd_start_game(base(games={"dayz":slot(was_used=True)}),"valheim")=="rejected" and not gstop,"9c RAM knapp + dayz(3P) -> valheim rejected, dayz bleibt")
# 9d RESERVIERTES, aber LEERES dayz weicht nicht, der Schutz gilt gerade dann, wenn niemand
# drauf ist. Ohne ihn waere 9b der Normalfall und ein reserviertes Spiel jederzeit abraeumbar.
# (Hier stand bis 2026-08-23 der --force-Test: 'verdraengt auch Besetztes'. Genau das gibt es
# nicht mehr, deshalb prueft die Stelle jetzt das Gegenteil.)
m.game_players=lambda g:0
gstop=[]
chk(m.cmd_start_game(base(games={"dayz":slot(geschuetzt=True)}),"valheim")=="rejected" and not gstop,"9d reserviertes leeres dayz -> valheim rejected, dayz bleibt")
# 9e reserviertes Lab bleibt Vorrang, unabhaengig vom freien Speicher (Owner-Ansage)
m.free_mb=lambda:8000; m.game_active=lambda g:False; m.lab_running=lambda:[210]
chk(m.cmd_start_game(base(reservation="lab"),"dayz")=="rejected","9e reserviertes Lab -> dayz rejected")
# 9f laufendes Spiel wird nicht neu gestartet (der Bot ruft /wake auch beim Nachsehen)
m.lab_running=lambda:[]; m.game_active=lambda g:g["name"]=="dayz"
started=[]; m.game_start=lambda g,world=None:started.append(g["name"])
chk(m.cmd_start_game(base(games={"dayz":slot(was_used=True)}),"dayz")=="already-running" and not started,"9f laeuft bereits -> kein Neustart")
# 9g --force existiert nicht mehr: der Aufruf bricht mit rc=2 ab, statt still zu verdraengen.
# Muskelgedaechtnis und alte Skripte sollen eine Absage sehen, keinen Scheinerfolg.
import subprocess, os
_rc=subprocess.run(["python3",os.environ["ARB_PY"],"--wake","dayz","--force"],
                   capture_output=True,text=True)
chk(_rc.returncode==2 and "--force gibt es nicht mehr" in _rc.stderr,"9g --force -> Abbruch mit Hinweis (rc=2)")
print("  T9: %d PASS / %d FAIL"%(P,F)); import sys; sys.exit(1 if F else 0)
PYEOF
[ $? -eq 0 ] && ok "T9 Mehrere Spiele gleichzeitig gruen" || no "T9 Mehrere Spiele"

echo; echo "=== T10: MC on-demand (isoliert, gestubbt), reservationsbasiert wie ein Game ==="
python3 - <<'PYEOF'
import importlib.machinery, time
m = importlib.machinery.SourceFileLoader("arb", __import__("os").environ["ARB_PY"]).load_module()
m.PROFILE={"node":"test","roles":{"minecraft":True,"lab":True}}  # Rollen der Tests, nicht die des Wirts
m.DRY=True; m.save_state=lambda s:None; m.emit_state=lambda *a,**k:None; m.audit=lambda msg:None
m.ensure_gate=lambda:None; m.gate_running=lambda:True; m.lab_running=lambda:[]; m.free_mb=lambda:8000
m.mc_lxc_running=lambda:True; m.mc_exit_code=lambda:0; m.GAMES=[]
P=F=0
def chk(c,l):
    global P,F
    print(("  [PASS] " if c else "  [FAIL] ")+l); P+=c; F+=(not c)
base=lambda **kw: dict({"mode":"IDLE","reservation":"none","restart_count":0,"idle_since":None,
  "lab_since":None,"lab_idle_since":None,"mc_idle_since":None,"mc_was_used":False,
  "game_idle_since":None,"game_was_used":False},**kw)
# 10a res=none + MC laeuft -> weicht (mc_stop)
m.mc_ct_state=lambda:"running"; m.mc_health=lambda:"healthy"; m.mc_players=lambda:0
st={"v":False}; m.mc_stop=lambda:st.update(v=True) or True; m.mc_start=lambda:True
s=m.tick(base(reservation="none",mode="MINECRAFT_ACTIVE"),False,False)
chk(st["v"] and s["mode"]=="MINECRAFT_STOPPING","10a res=none + MC laeuft -> weicht")
# 10b res=none + MC aus -> IDLE, KEIN Start (kein immer-online)
m.mc_ct_state=lambda:"exited"; m.mc_health=lambda:"n/a"; m.mc_players=lambda:-1
sa={"v":False}; m.mc_start=lambda:sa.update(v=True) or True
s=m.tick(base(reservation="none"),False,False)
chk(not sa["v"] and s["mode"]=="IDLE","10b res=none + MC aus -> IDLE, kein Start")
# 10c res=minecraft + MC aus -> Start
sa={"v":False}; m.mc_start=lambda:sa.update(v=True) or True
s=m.tick(base(reservation="minecraft"),False,False)
chk(sa["v"] and s["mode"]=="MINECRAFT_STARTING","10c res=minecraft + MC aus -> Start")
# 10d idle-auto-off: reserviert + healthy + leer + genutzt + idle>timeout -> auto-off
m.mc_ct_state=lambda:"running"; m.mc_health=lambda:"healthy"; m.mc_players=lambda:0
st={"v":False}; m.mc_stop=lambda:st.update(v=True) or True
s=m.tick(base(reservation="minecraft",mode="MINECRAFT_ACTIVE",mc_was_used=True,mc_idle_since=time.time()-9999),False,False)
chk(st["v"] and s["reservation"]=="none","10d leer>timeout nach Nutzung -> auto-off (res=none)")
# 10e cmd_wake_mc frei -> started; cmd_sleep_mc -> stopped
m.mc_ct_state=lambda:"exited"; m.blocking_role=lambda st,**k:None; m.mc_start=lambda:True
s=base(); r1=m.cmd_wake_mc(s)
m.mc_ct_state=lambda:"running"; m.mc_stop=lambda:True; r2=m.cmd_sleep_mc(s)
chk(r1=="started" and r2=="stopped" and s["reservation"]=="none","10e cmd_wake_mc/sleep_mc Zyklus")
# 10f cmd_wake_mc + belegte Rolle -> rejected (es gibt keinen Override mehr)
m.mc_ct_state=lambda:"exited"; m.blocking_role=lambda st,**k:{"kind":"game","name":"dayz","players":2}
s=base(); chk(m.cmd_wake_mc(s)=="rejected" and s["reservation"]=="none","10f belegte Rolle -> wake_mc rejected")
print("  T10: %d PASS / %d FAIL"%(P,F)); import sys; sys.exit(1 if F else 0)
PYEOF
[ $? -eq 0 ] && ok "T10 MC on-demand gruen" || no "T10 MC on-demand"

echo; echo "=== T11: Welt-Vorbereitung ruft das Skript DES SPIELS (isoliert, gestubbt) ==="
python3 - <<'PYEOF'
import importlib.machinery
m = importlib.machinery.SourceFileLoader("arb", __import__("os").environ["ARB_PY"]).load_module()
m.PROFILE={"node":"test","roles":{"minecraft":False,"lab":False}}
m.DRY=True; m.save_state=lambda s:None; m.emit_state=lambda *a,**k:None
P=F=0
def chk(c,l):
    global P,F
    print(("  [PASS] " if c else "  [FAIL] ")+l); P+=c; F+=(not c)
# act() protokollieren statt ausfuehren -> wir sehen den exakten Befehl, den der Arbiter absetzen wuerde.
CMDS=[]
m.act=lambda label,cmd,**kw: CMDS.append(cmd) or True
LOG=[]; m.audit=lambda msg: LOG.append(msg)
TERR={"name":"terraria","kind":"systemd","service":"terraria-server","multi_world":True,
      "world_script":"/usr/local/bin/ensure-world-terraria.sh"}
ZOMB={"name":"zomboid","kind":"systemd","service":"zomboid-server","multi_world":True,
      "world_script":"/usr/local/bin/ensure-world-zomboid.sh"}
ALT ={"name":"altspiel","kind":"systemd","service":"alt-server","multi_world":True}
m.GAMES=[TERR,ZOMB,ALT]
m.world_info=lambda n:("greenleaf",["greenleaf","solo"])
# 11a/11b: zwei Spiele auf EINEM Wirt duerfen nie dasselbe Skript rufen, das war der Fehler,
# der auf gamehost Terrarias serverconfig.txt ueberschrieben haette.
CMDS[:]=[]; m.game_start(TERR, world="solo")
t_ok = any("ensure-world-terraria.sh solo" in c for c in CMDS)
CMDS[:]=[]; m.game_start(ZOMB, world="solo")
z_ok = any("ensure-world-zomboid.sh solo" in c for c in CMDS)
chk(t_ok, "11a terraria ruft ensure-world-terraria.sh")
chk(z_ok, "11b zomboid ruft ensure-world-zomboid.sh (nicht Terrarias)")
# 11c: ohne Feld bleibt der alte Sammelpfad -> Bestand auf Node .18 aendert sich nicht.
CMDS[:]=[]; m.game_start(ALT, world="greenleaf")
chk(any(c.startswith("/usr/local/bin/ensure-world.sh greenleaf") for c in CMDS),
    "11c ohne world_script -> alter Default (rueckwaertskompatibel)")
# 11d: die ID landet in einer Shell. Ein Wert, der nie durch die Registry-Pruefung kam,
# darf hier nicht durchrutschen -- weder ausgefuehrt noch stillschweigend ignoriert.
CMDS[:]=[]; LOG[:]=[]
r = m.game_start(ZOMB, world="../../etc; rm -rf /")
chk(r is False and not CMDS and any("ABGELEHNT" in x for x in LOG),
    "11d ungueltige Welt-ID -> abgelehnt, kein Befehl abgesetzt")
print("  T11: %d PASS / %d FAIL"%(P,F)); import sys; sys.exit(1 if F else 0)
PYEOF
[ $? -eq 0 ] && ok "T11 Welt-Skript-Auswahl gruen" || no "T11 Welt-Skript-Auswahl"

# ACHTUNG, teuer gelernt am 2026-08-22: dieser Aufraeum-Block lief frueher IMMER -- auch dort,
# wo die Live-Bloecke uebersprungen wurden. Sein '$ARB --live' ist aber kein Aufraeumen, sondern
# ein echter Tick gegen den echten Zustand: auf dem Spiele-VPS stoppte er prompt alle vier
# laufenden Server. Eine Testsuite darf den Wirt nicht anfassen, den sie nur pruefen soll.
if [ "$LIVE_MOEGLICH" = "1" ]; then
  echo; echo "=== Cleanup: on-demand-Grundzustand, MC schlafen, Node idle, Gate up ==="
  $ARB --release >/dev/null 2>&1; $ARB --reset >/dev/null 2>&1
  $ARB --live --stop-mc >/dev/null 2>&1; $ARB --live >/dev/null 2>&1; sleep 3
  echo "  Endzustand: mc=$(ctstate) gate=$(gatestate) mode=$(mode)  (MC on-demand: /minecraft Start weckt)"
else
  echo; echo "=== Kein Cleanup: ohne Live-Bloecke wurde nichts veraendert, was aufzuraeumen waere ==="
fi
echo; echo "############ ERGEBNIS: $PASS PASS / $FAIL FAIL ############"
