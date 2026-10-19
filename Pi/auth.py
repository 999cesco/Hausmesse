"""
Anmeldung fuer das Dashboard.

- Passwoerter werden nur als Hash gespeichert (scrypt, ueber werkzeug), nie im Klartext.
- Benutzer stehen in benutzer.json, der Sitzungsschluessel in .flask_secret.
  Beide Dateien ignoriert Git - sie landen also nie im oeffentlichen Repo.
- Nach 5 falschen Versuchen ist die Anmeldung fuer diese Adresse 15 Minuten gesperrt.
- Konten koennen gesperrt werden oder ein Ablaufdatum haben (z. B. Gastzugang fuer die Messe).
- Normale Nutzer haben Rechte (welche Geraete, KI-Chat, Limit) - siehe rechte.py.
- Aendert sich ein Passwort oder wird ein Benutzer geloescht, sind seine Sitzungen sofort ungueltig.

Benutzer verwalten (auf dem Pi, im Ordner Pi):
    ~/venv/bin/python3 auth.py anlegen <name> admin     # Administrator
    ~/venv/bin/python3 auth.py anlegen <name> nutzer    # normaler Nutzer
    ~/venv/bin/python3 auth.py passwort <name>
    ~/venv/bin/python3 auth.py loeschen <name>
    ~/venv/bin/python3 auth.py liste
    ~/venv/bin/python3 auth.py entsperren <name>        # falls man sich ausgesperrt hat
"""

import json
import os
import re
import secrets
import sys
import threading
import time
from datetime import date
from functools import wraps

from flask import jsonify, redirect, request, session
from werkzeug.security import check_password_hash, generate_password_hash

import rechte

BASE = os.path.dirname(os.path.abspath(__file__))
USERS_FILE = os.path.join(BASE, "benutzer.json")
SECRET_FILE = os.path.join(BASE, ".flask_secret")

ROLES = ("admin", "nutzer")
MIN_PASSWORD = 8
MAX_FAILS = 5
LOCK_SECONDS = 15 * 60

_lock = threading.Lock()
_fails = {}   # Adresse -> Liste von Zeitpunkten fehlgeschlagener Versuche

# Wird geprueft, wenn es den Benutzer nicht gibt - damit dauert die Antwort gleich lang
# und man kann nicht an der Zeit erkennen, ob ein Benutzername existiert.
_DUMMY_HASH = generate_password_hash("dummy-passwort-zum-zeitausgleich")


# -------------------- Dateien --------------------

def _write_private(path, text):
    """Schreibt atomar und nur fuer den eigenen Benutzer lesbar (chmod 600)."""
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def secret_key():
    if not os.path.exists(SECRET_FILE):
        _write_private(SECRET_FILE, secrets.token_hex(32))
    with open(SECRET_FILE, encoding="utf-8") as f:
        return f.read().strip()


def load_users():
    try:
        with open(USERS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_users(users):
    _write_private(USERS_FILE, json.dumps(users, indent=2, ensure_ascii=False))


# -------------------- Benutzer --------------------

def check_name(name):
    if not re.fullmatch(r"[a-z0-9_.-]{2,32}", name or ""):
        raise ValueError("Benutzername: 2-32 Zeichen, nur Kleinbuchstaben, Ziffern, _ . -")


def check_password(pw):
    if not isinstance(pw, str) or len(pw) < MIN_PASSWORD:
        raise ValueError(f"Das Passwort muss mindestens {MIN_PASSWORD} Zeichen lang sein.")
    if len(pw) > 200:
        raise ValueError("Das Passwort ist zu lang.")


def add_user(name, password, role):
    check_name(name)
    check_password(password)
    if role not in ROLES:
        raise ValueError("Rolle muss 'admin' oder 'nutzer' sein.")
    with _lock:
        users = load_users()
        if name in users:
            raise ValueError("Diesen Benutzer gibt es schon.")
        users[name] = {"hash": generate_password_hash(password), "rolle": role, "version": 1,
                       "gesperrt": False, "gueltig_bis": "", "rechte": rechte.default()}
        _save_users(users)


def set_password(name, password):
    check_password(password)
    with _lock:
        users = load_users()
        if name not in users:
            raise ValueError("Diesen Benutzer gibt es nicht.")
        users[name]["hash"] = generate_password_hash(password)
        users[name]["version"] = users[name].get("version", 1) + 1   # alte Sitzungen ungueltig
        _save_users(users)


def delete_user(name):
    with _lock:
        users = load_users()
        if name not in users:
            raise ValueError("Diesen Benutzer gibt es nicht.")
        rest = {n: u for n, u in users.items() if n != name}
        if _active_admins(users) and not _active_admins(rest):
            raise ValueError("Der letzte Administrator kann nicht gelöscht werden.")
        del users[name]
        _save_users(users)


def _expired(user):
    until = user.get("gueltig_bis") or ""
    return bool(until) and date.today().isoformat() > until


def _active(user):
    return not user.get("gesperrt") and not _expired(user)


def _active_admins(users):
    return [n for n, u in users.items() if u["rolle"] == "admin" and _active(u)]


def check_date(value):
    if value in ("", None):
        return ""
    try:
        return date.fromisoformat(str(value)).isoformat()
    except ValueError:
        raise ValueError("Gültig bis: bitte ein Datum wählen")


def update_user(name, rolle=None, gesperrt=None, gueltig_bis=None, rechte_neu=None):
    """Aendert Rolle, Sperre, Ablaufdatum und Rechte. Alles wird zuerst geprueft."""
    with _lock:
        users = load_users()
        if name not in users:
            raise ValueError("Diesen Benutzer gibt es nicht.")
        u = dict(users[name])
        if rolle is not None:
            if rolle not in ROLES:
                raise ValueError("Rolle muss 'admin' oder 'nutzer' sein.")
            u["rolle"] = rolle
        if gesperrt is not None:
            u["gesperrt"] = bool(gesperrt)
        if gueltig_bis is not None:
            u["gueltig_bis"] = check_date(gueltig_bis)
        if rechte_neu is not None:
            u["rechte"] = rechte.normalize(rechte_neu)
        changed = {**users, name: u}
        if _active_admins(users) and not _active_admins(changed):
            raise ValueError("Es muss mindestens ein aktiver Administrator bleiben.")
        users[name] = u
        _save_users(users)


def _public(name, u):
    return {"name": name, "rolle": u["rolle"], "gesperrt": bool(u.get("gesperrt")),
            "gueltig_bis": u.get("gueltig_bis", ""), "abgelaufen": _expired(u),
            "rechte": rechte.upgrade(u.get("rechte")) or rechte.default()}


def list_users():
    return [_public(n, u) for n, u in sorted(load_users().items())]


# -------------------- Anmeldung --------------------

def client_ip():
    ip = request.remote_addr or "?"
    # Nur wenn die Anfrage vom Pi selbst kommt (z. B. ueber Tailscale Funnel),
    # der weitergereichten Adresse vertrauen - sonst koennte man sie faelschen.
    if ip in ("127.0.0.1", "::1") and request.headers.get("X-Forwarded-For"):
        ip = request.headers["X-Forwarded-For"].split(",")[0].strip()
    return ip


def locked_seconds(ip):
    now = time.time()
    with _lock:
        recent = [t for t in _fails.get(ip, []) if now - t < LOCK_SECONDS]
        _fails[ip] = recent
        if len(recent) >= MAX_FAILS:
            return int(LOCK_SECONDS - (now - recent[0]))
    return 0


def login(name, password):
    """Gibt (ok, fehlermeldung) zurueck und setzt bei Erfolg die Sitzung."""
    ip = client_ip()
    wait = locked_seconds(ip)
    if wait:
        return False, f"Zu viele Fehlversuche. Versuche es in {wait // 60 + 1} Minuten wieder."

    users = load_users()
    user = users.get((name or "").strip().lower())
    ok = check_password_hash(user["hash"] if user else _DUMMY_HASH, password or "")
    if not (user and ok):
        with _lock:
            _fails.setdefault(ip, []).append(time.time())
        time.sleep(1)   # bremst Durchprobieren zusaetzlich
        print(f"[AUTH] Fehlgeschlagene Anmeldung fuer '{name}' von {ip}")
        return False, "Benutzername oder Passwort ist falsch."

    with _lock:
        _fails.pop(ip, None)
    if user.get("gesperrt"):
        return False, "Dieses Konto ist gesperrt. Wende dich an einen Administrator."
    if _expired(user):
        return False, "Dieser Zugang ist abgelaufen. Wende dich an einen Administrator."
    session.clear()
    session.permanent = True
    session["user"] = name.strip().lower()
    session["version"] = user.get("version", 1)
    print(f"[AUTH] '{session['user']}' angemeldet von {ip}")
    return True, ""


def current_user():
    """Prueft die Sitzung bei jeder Anfrage gegen die Benutzerdatei."""
    name = session.get("user")
    if not name:
        return None
    user = load_users().get(name)
    if not user or user.get("version", 1) != session.get("version") or not _active(user):
        session.clear()       # Passwort geaendert, geloescht, gesperrt oder abgelaufen
        return None
    return {"name": name, "rolle": user["rolle"],
            # Admins duerfen alles -> keine Einschraenkungen
            "rechte": None if user["rolle"] == "admin" else (rechte.upgrade(user.get("rechte")) or rechte.default())}


def _deny(status, msg):
    if request.path.startswith("/api/"):
        return jsonify({"error": msg}), status
    return redirect("/login")


def login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not current_user():
            return _deny(401, "Bitte anmelden.")
        return view(*args, **kwargs)
    return wrapper


def admin_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        user = current_user()
        if not user:
            return _deny(401, "Bitte anmelden.")
        if user["rolle"] != "admin":
            return jsonify({"error": "Nur für Administratoren."}), 403
        return view(*args, **kwargs)
    return wrapper


# -------------------- Kommandozeile --------------------

def _ask_password():
    import getpass
    while True:
        pw = getpass.getpass("Passwort: ")
        if getpass.getpass("Passwort wiederholen: ") != pw:
            print("Die Passwörter stimmen nicht überein.")
            continue
        try:
            check_password(pw)
            return pw
        except ValueError as e:
            print(e)


def _cli(args):
    try:
        if len(args) >= 2 and args[0] == "anlegen":
            role = args[2] if len(args) > 2 else "nutzer"
            add_user(args[1], _ask_password(), role)
            print(f"Benutzer '{args[1]}' ({role}) angelegt.")
        elif len(args) == 2 and args[0] == "passwort":
            set_password(args[1], _ask_password())
            print("Passwort geändert.")
        elif len(args) == 2 and args[0] == "loeschen":
            delete_user(args[1])
            print("Benutzer gelöscht.")
        elif len(args) == 2 and args[0] == "entsperren":
            update_user(args[1], gesperrt=False, gueltig_bis="")
            print("Benutzer entsperrt.")
        elif args[:1] == ["liste"]:
            for u in list_users():
                info = " gesperrt" if u["gesperrt"] else (" abgelaufen" if u["abgelaufen"] else "")
                print(f"  {u['name']:20} {u['rolle']}{info}")
            if not list_users():
                print("  (noch keine Benutzer)")
        else:
            print(__doc__)
    except ValueError as e:
        print(f"Fehler: {e}")
        sys.exit(1)


if __name__ == "__main__":
    _cli(sys.argv[1:])
