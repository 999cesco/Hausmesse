"""
KI-Training: Testprogramm und Verbesserungs-Loop.

- testfaelle.json enthaelt typische Saetze und was dabei herauskommen MUSS.
  Bewertet wird vom Code (welche Befehle, welche Art, Stichworte in der Antwort) - nicht von der KI.
- "Nur testen" schickt alle Faelle mit den aktuellen Zusatzregeln an die KI.
- "Training" laesst die KI aus den Fehlschlaegen verbesserte Zusatzregeln vorschlagen.
  Ein Vorschlag wird nur behalten, wenn MEHR Tests bestehen und KEIN vorher bestandener kaputtgeht.
- Ein Teil der Faelle ist "Kontrolle": deren Fehler sieht die KI beim Verbessern nie. So merkt man,
  ob die Regeln wirklich besser werden oder nur die Testsaetze auswendig lernen.
- Live geht ein Ergebnis erst, wenn ein Admin es uebernimmt.
- Waehrend Test oder Training laufen, beantwortet die KI keine Nutzerfragen (siehe schnell.antworte).

Dieses Modul ist der "Motor". Er laeuft normalerweise auf dem SERVER (server/trainingsdienst.py),
damit auch trainiert werden kann, wenn der Pi aus ist. Der Pi spricht ihn ueber training_fern.py an.
"""

import json
import os
import re
import threading
import time

import requests

import config
import ki

BASE = os.path.dirname(os.path.abspath(__file__))
CASES_FILE = os.path.join(BASE, "testfaelle.json")
RESULT_FILE = os.path.join(BASE, "training_ergebnis.json")
MAX_ZUSATZ = 1500          # Zeichen - laengere Regeln machen das kleine Modell eher schlechter
SEED = 42                  # gleiche Frage + gleicher Prompt -> gleiche Antwort (vergleichbare Laeufe)

_lock = threading.Lock()
STATE = {
    "laeuft": False, "modus": "", "quelle": "", "runde": 0, "runden": 0, "fall": 0, "faelle": 0,
    "stop": False, "seit": 0,
    "live": None,            # letztes Testergebnis mit den Live-Regeln
    "kandidat": None,        # {"zusatz": ..., "ergebnis": ...} - wartet auf Admin
    "log": [],               # Verlauf der Runden
    "fehler": "",
}


def busy():
    return STATE["laeuft"]


def _save():
    data = {k: STATE[k] for k in ("live", "kandidat", "log")}
    tmp = RESULT_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, RESULT_FILE)


def _load():
    try:
        with open(RESULT_FILE, encoding="utf-8") as f:
            STATE.update(json.load(f))
    except (FileNotFoundError, ValueError):
        pass


_load()


def _log(text):
    STATE["log"] = (STATE["log"] + [f"{time.strftime('%H:%M')}  {text}"])[-40:]
    print(f"[TRAINING] {text}")


# -------------------- Testfaelle --------------------

def load_cases():
    with open(CASES_FILE, encoding="utf-8") as f:
        data = json.load(f)
    cases = data["faelle"]
    for i, c in enumerate(cases):
        c.setdefault("id", f"t{i + 1:02d}")
        c.setdefault("gruppe", "kontrolle" if i % 4 == 3 else "training")
    return data["zustaende"], cases


def _history(verlauf):
    msgs = []
    for v in verlauf or []:
        out = {"roh": {"art": v.get("art", "sonstiges"), "szene": "", "rueckfrage": v.get("rueckfrage", False)},
               "antwort": v["antwort"], "befehle": v.get("befehle", [])}
        msgs += ki.history_entries(v["nutzer"], out)
    return msgs


def _cmd_matches(c, e):
    if c.get("device") != e["device"]:
        return False
    if "room" in e and c.get("room") != e["room"]:
        return False
    actions = e.get("action")
    if actions and c.get("action") not in (actions if isinstance(actions, list) else [actions]):
        return False
    if "value" in e and (c.get("value") is None or abs(float(c["value"]) - e["value"]) > 0.6):
        return False
    return True


def _has(cmds, e):
    if any(_cmd_matches(c, e) for c in cmds):
        return True
    # "Alle Lichter" darf auch als einzelne Befehle fuer jeden Raum kommen
    if e.get("device") == "light" and e.get("room") == "alle":
        return all(any(_cmd_matches(c, {**e, "room": r}) for c in cmds) for r in ki.LIGHT_ROOMS)
    return False


def _fmt(cmds):
    act = lambda a: "/".join(a) if isinstance(a, list) else str(a)
    return ", ".join(f"{c['device']}{'/' + c['room'] if c.get('room') else ''} {act(c.get('action'))}"
                     f"{' ' + str(c['value']) if c.get('value') is not None else ''}" for c in cmds) or "keine"


def grade(case, out):
    """Prueft eine Antwort gegen die Erwartung. Gibt '' (bestanden) oder den Grund zurueck."""
    e, cmds, antwort = case["erwartet"], out["befehle"], out["antwort"]
    low = antwort.lower()
    if "art" in e and out["art"] not in e["art"]:
        return f"Art '{out['art']}' statt {'/'.join(e['art'])}"
    if e.get("keine_befehle") and cmds:
        return f"hat geschaltet, obwohl es nicht sollte: {_fmt(cmds)}"
    for x in e.get("befehle", []):
        if not _has(cmds, x):
            return f"Befehl fehlt ({_fmt([x])}), bekam: {_fmt(cmds)}"
    if e.get("irgendein") and not any(_has(cmds, x) for x in e["irgendein"]):
        return f"keiner der erwarteten Befehle, bekam: {_fmt(cmds)}"
    for x in e.get("nicht", []):
        if _has(cmds, x):
            return f"falscher Befehl: {_fmt([x])}"
    if "szene" in e and out["szene"] != e["szene"]:
        return f"Szene '{out['szene'] or 'keine'}' statt '{e['szene']}'"
    if e.get("enthaelt") and not any(w.lower() in low for w in e["enthaelt"]):
        return f"Antwort enthält keins von: {', '.join(e['enthaelt'])}"
    if e.get("regex") and not re.search(e["regex"], antwort, re.IGNORECASE):
        return "Antwort hat nicht die erwartete Form"
    if len(antwort.strip()) < e.get("min_laenge", 1):
        return "Antwort leer oder zu kurz"
    if "rueckfrage" in e and out["rueckfrage"] != e["rueckfrage"]:
        return "Rückfrage-Markierung falsch"
    return ""


def run_suite(zusatz, label):
    """Schickt alle Testfaelle mit diesen Zusatzregeln an die KI. Schaltet nichts."""
    zustaende, cases = load_cases()
    prompt = ki.build_prompt(zusatz)
    STATE.update(faelle=len(cases), fall=0)
    results = {}
    start = time.time()
    for i, c in enumerate(cases):
        if STATE["stop"]:
            return None
        STATE["fall"] = i + 1
        try:
            raw, res = ki._ask_model(c["text"], zustaende[c.get("zustand", "normal")],
                                     history=_history(c.get("verlauf")), prompt=prompt, seed=SEED)
            out = ki.interpret(res)
            grund = grade(c, out)
        except Exception as ex:                                  # Server weg, kaputtes JSON ...
            out, grund = {"antwort": "", "befehle": [], "art": ""}, f"Fehler: {type(ex).__name__}"
        results[c["id"]] = {"ok": not grund, "grund": grund, "text": c["text"], "gruppe": c["gruppe"],
                            "antwort": out["antwort"], "befehle": _fmt(out["befehle"]), "art": out["art"]}
    def score(g):
        r = [x for x in results.values() if g is None or x["gruppe"] == g]
        return sum(x["ok"] for x in r), len(r)
    summary = {"zusatz": zusatz, "label": label, "zeit": time.strftime("%d.%m. %H:%M"),
               "dauer_s": int(time.time() - start), "gesamt": score(None),
               "training": score("training"), "kontrolle": score("kontrolle"), "ergebnisse": results}
    _log(f"{label}: {summary['gesamt'][0]}/{summary['gesamt'][1]} bestanden "
         f"(Training {summary['training'][0]}/{summary['training'][1]}, "
         f"Kontrolle {summary['kontrolle'][0]}/{summary['kontrolle'][1]}), {summary['dauer_s']} s")
    return summary


# -------------------- Verbesserungsvorschlag --------------------

META_PROMPT = """Du verbesserst die ZUSATZREGELN eines kleinen deutschen Sprachassistenten fuer ein Smarthome.
Der Assistent bekommt einen festen Systemprompt (unten) und darunter die Zusatzregeln, die Vorrang haben.
Bei einigen Testsaetzen hat er falsch reagiert. Schreib die Zusatzregeln so um, dass er diese Fehler
nicht mehr macht, ohne dass etwas anderes schlechter wird.

Vorgaben fuer die neuen Zusatzregeln:
- Hoechstens {max} Zeichen, kurze Stichpunkte, jede Regel beginnt mit "- ".
- Allgemeine Regeln formulieren, die auch fuer aehnliche Saetze gelten. Die Testsaetze NICHT woertlich
  abschreiben und keine Liste von Einzelfaellen anlegen.
- Bestehende Zusatzregeln behalten, wenn sie nicht offensichtlich schuld an einem Fehler sind.
- Nichts am Antwortformat (JSON) aendern und nichts verlangen, was dem Systemprompt widerspricht.

=== SYSTEMPROMPT DES ASSISTENTEN ===
{system}
=== ENDE SYSTEMPROMPT ===
"""

PROPOSAL_SCHEMA = {"type": "object", "properties": {
    "analyse": {"type": "string"}, "zusatzregeln": {"type": "string"}}, "required": ["analyse", "zusatzregeln"]}


def propose(zusatz, summary, round_no):
    fails = [r for r in summary["ergebnisse"].values() if not r["ok"] and r["gruppe"] == "training"]
    lines = [f"- Satz: \"{r['text']}\"\n  Problem: {r['grund']}\n  Er sagte: \"{r['antwort'][:160]}\" "
             f"(Art {r['art'] or '?'}, Befehle: {r['befehle']})" for r in fails[:12]]
    user = (f"AKTUELLE ZUSATZREGELN:\n{zusatz or '(noch keine)'}\n\nFEHLER BEI DEN TESTS:\n" + "\n".join(lines) +
            "\n\nAntworte mit einer kurzen Analyse der Ursachen und den vollstaendigen neuen Zusatzregeln.")
    r = requests.post(config.OLLAMA_URL, json={
        "model": config.MODEL, "stream": False, "format": PROPOSAL_SCHEMA, "keep_alive": "30m",
        "options": {"temperature": 0.7, "num_ctx": 8192, "seed": SEED + round_no},
        "messages": [{"role": "system", "content": META_PROMPT.format(max=MAX_ZUSATZ, system=ki.SYSTEM_PROMPT)},
                     {"role": "user", "content": user}],
    }, timeout=300, **config.server_args())
    r.raise_for_status()
    data = json.loads(r.json()["message"]["content"])
    neu = data.get("zusatzregeln", "").strip()[:MAX_ZUSATZ]
    _log(f"Runde {round_no}: Idee der KI: {data.get('analyse', '')[:160]}")
    return neu


def _regressions(old, new):
    return [k for k, r in old["ergebnisse"].items() if r["ok"] and not new["ergebnisse"].get(k, {}).get("ok")]


# -------------------- Ablauf --------------------

def _run(modus, runden, base):
    try:
        live_zusatz = ki.load_zusatz()
        start_zusatz = live_zusatz if base is None else base
        best = run_suite(start_zusatz, "Aktuelle Regeln" if base is None else "Ausgangspunkt (letzter Vorschlag)")
        if best is None:
            return
        if base is None:
            STATE["live"] = best
        _save()
        if modus == "test":
            return
        start_score = best["training"][0]
        for n in range(1, runden + 1):
            if STATE["stop"]:
                break
            STATE["runde"] = n
            if best["training"][0] == best["training"][1]:
                _log("Alle Trainingsfälle bestehen - fertig.")
                break
            try:
                neu = propose(best["zusatz"], best, n)
            except Exception as ex:
                _log(f"Runde {n}: kein Vorschlag ({type(ex).__name__})")
                continue
            if not neu or neu == best["zusatz"]:
                _log(f"Runde {n}: Vorschlag unverändert, verworfen")
                continue
            res = run_suite(neu, f"Runde {n}")
            if res is None:
                break
            kaputt = _regressions(best, res)
            if res["training"][0] > best["training"][0] and not kaputt:
                best = res
                _log(f"Runde {n}: besser - behalten")
            else:
                why = f"{len(kaputt)} vorher bestandene Tests kaputt" if kaputt else "nicht besser"
                _log(f"Runde {n}: {why} - verworfen")
        STATE["verbessert"] = best["training"][0] > start_score
        if best["zusatz"] != live_zusatz and (STATE["verbessert"] or base is not None):
            STATE["kandidat"] = {"zusatz": best["zusatz"], "ergebnis": best}
            _log("Neue Regeln gefunden - warten auf Übernahme im Admin-Bereich."
                 if STATE["verbessert"] else "Keine weitere Verbesserung, Vorschlag bleibt.")
        else:
            _log("Keine bessere Fassung gefunden.")
        _save()
    except Exception as ex:
        STATE["fehler"] = f"{type(ex).__name__}: {ex}"
        _log(f"Abgebrochen: {STATE['fehler']}")
    finally:
        if STATE["stop"]:
            _log("Angehalten.")
        STATE.update(laeuft=False, stop=False, modus="", runde=0, quelle="")


def start(modus="training", runden=10, quelle="manuell", base=None):
    """base: Zusatzregeln, von denen aus verbessert wird (Standard: die Live-Regeln)."""
    with _lock:
        if STATE["laeuft"]:
            raise ValueError("Es läuft schon ein Test oder Training.")
        load_cases()                                  # Datei kaputt? Dann gleich hier melden
        STATE.update(laeuft=True, modus=modus, quelle=quelle, runde=0,
                     runden=runden if modus == "training" else 0,
                     stop=False, seit=time.time(), fehler="", fall=0, verbessert=False)
    wer = "automatisch" if quelle == "automatisch" else "vom Admin"
    _log(f"Test gestartet ({wer})" if modus == "test" else f"Training gestartet ({wer}, {runden} Runden)")
    threading.Thread(target=_run, args=(modus, runden, base), daemon=True).start()


def stop():
    if STATE["laeuft"]:
        STATE["stop"] = True


def accept():
    k = STATE["kandidat"]
    if not k:
        raise ValueError("Es gibt keinen Vorschlag.")
    with open(ki.ZUSATZ_FILE, "w", encoding="utf-8") as f:
        f.write(k["zusatz"])
    STATE["live"], STATE["kandidat"] = k["ergebnis"], None
    _log("Neue Regeln übernommen - ab jetzt live.")
    _save()


def discard():
    STATE["kandidat"] = None
    _log("Vorschlag verworfen.")
    _save()


def set_live(zusatz):
    """Live-Regeln setzen (der Pi meldet hier, was bei ihm gerade gilt)."""
    zusatz = (zusatz or "").strip()[:MAX_ZUSATZ * 2]
    if zusatz == ki.load_zusatz():
        return
    if zusatz:
        with open(ki.ZUSATZ_FILE, "w", encoding="utf-8") as f:
            f.write(zusatz)
    elif os.path.exists(ki.ZUSATZ_FILE):
        os.remove(ki.ZUSATZ_FILE)
    STATE["live"] = None                      # altes Testergebnis passt nicht mehr


def reset_rules():
    if os.path.exists(ki.ZUSATZ_FILE):
        os.remove(ki.ZUSATZ_FILE)
    STATE["live"] = None
    _log("Zusatzregeln gelöscht - es gilt nur der Grundprompt.")
    _save()


def status():
    def short(s):
        if not s:
            return None
        fails = [r for r in s["ergebnisse"].values() if not r["ok"]]
        return {k: s[k] for k in ("zeit", "dauer_s", "gesamt", "training", "kontrolle", "label")} | {"fehler": fails}
    return {
        "laeuft": STATE["laeuft"], "modus": STATE["modus"], "quelle": STATE["quelle"],
        "runde": STATE["runde"], "runden": STATE["runden"],
        "fall": STATE["fall"], "faelle": STATE["faelle"], "seit_s": int(time.time() - STATE["seit"]) if STATE["laeuft"] else 0,
        "fehler": STATE["fehler"], "log": STATE["log"][-15:],
        "live": short(STATE["live"]), "live_zusatz": ki.load_zusatz(),
        "kandidat": {"zusatz": STATE["kandidat"]["zusatz"], "ergebnis": short(STATE["kandidat"]["ergebnis"])}
                    if STATE["kandidat"] else None,
    }
