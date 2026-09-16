#!/usr/bin/env python3
"""publish_sciebo.py - Dateien eines Artefakts in einen sciebo-Ordner spiegeln.

Ein Vertrag, sonst nichts:

    Jede Datei aus DIST_DIR landet unter ihrem eigenen Namen im Zielordner und
    ersetzt dort die gleichnamige Datei. Es wird nie etwas gelöscht, nie ein
    Ordner angelegt und nie ein Dateiname ausgewertet.

Daraus folgt alles Weitere. Weil nichts gelöscht wird, gibt es keine
Eigentumsfrage: kein Slug, keine Allowlist, keine Präfixregel, keine
Endungsliste, keine Versionsgrammatik. Der Aufrufer bestimmt die Namen, indem
er die Dateien im Artefakt so benennt, wie sie in sciebo heißen sollen.

Ersetzt wird in zwei Schritten, damit der Zielname nie eine halbe Datei trägt:
PUT auf ".upload.<name>", Bytes zurücklesen und vergleichen, dann MOVE mit
Overwrite auf "<name>". Der Zielname wechselt damit von einer vollständigen
Datei zur nächsten. Scheitert einer der Schritte, bleibt die alte Datei stehen
und die (im Web versteckte) Zwischendatei wird beim nächsten Lauf überschrieben.

Aufruf:
  publish_sciebo.py DIST_DIR              DIST_DIR/* hochladen
  publish_sciebo.py --dry-run [DIST_DIR]  nur lesen; zeigt, was passieren würde
  publish_sciebo.py --self-test           Offline-Prüfung der Schutzlogik, kein Netz

Umgebung:
  SCIEBO_BASE_URL      https://<host>, z.B. https://uni-muenster.sciebo.de   (Variable)
                       Das Skript ergänzt /remote.php/dav/files/<SCIEBO_USER>. Die volle
                       Dateien-Wurzel https://<host>/remote.php/dav/files/<login> wird
                       ebenfalls angenommen und dann unverändert benutzt.
  SCIEBO_FOLDER        Zielordner unterhalb der Dateien-Wurzel        (Variable)
  SCIEBO_USER          sciebo-Login                                   (Secret)
  SCIEBO_APP_PASSWORD  App-Passwort                                   (Secret)

Wird von MuensterEntrepreneurship/.github/.github/workflows/publish-sciebo.yml zur
Laufzeit an genau dem Commit nachgeladen, aus dem dieser Workflow selbst läuft.
"""

import base64
import contextlib
import hashlib
import io
import os
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

IN_CI = os.environ.get("GITHUB_ACTIONS") == "true"
ENV_VARS = ("SCIEBO_BASE_URL", "SCIEBO_FOLDER", "SCIEBO_USER", "SCIEBO_APP_PASSWORD")
DAV_FILES = "/remote.php/dav/files/"

# Die einzigen Methoden, die dieses Skript kennt. DELETE und MKCOL stehen
# bewusst NICHT darin: nichts wird gelöscht, und der Zielordner muss bereits
# existieren. Der Guard in Dav.request() greift vor jedem Socket.
ALLOWED_METHODS = ("PUT", "GET", "PROPFIND", "MOVE")

MAX_NAME = 200
# Der Zwischenname trägt ein Präfix, keine eigene Endung: Nextcloud lehnt einen
# PUT auf Namen mit der Endung .part rundweg mit HTTP 400 ab (die Endung ist für
# seinen eigenen Teil-Upload reserviert, forbidden_filename_extensions), und
# welche Endungen eine Instanz sonst noch sperrt, ist Konfigurationssache. Mit
# dem Präfix endet die Zwischendatei auf dieselbe Endung wie ihr Ziel: was als
# Ziel erlaubt ist, ist damit auch als Zwischenstand erlaubt. Der führende Punkt
# blendet sie in der Weboberfläche aus.
UPLOAD_PREFIX = ".upload"


def log(msg):
    print(msg, flush=True)


def warn(msg, title="publish-sciebo"):
    log(f"::warning title={title}::{msg}" if IN_CI else f"WARNUNG: {msg}")


def die(msg, code=1):
    log(f"::error::{msg}" if IN_CI else f"FEHLER: {msg}")
    sys.exit(code)


# --- Namen ------------------------------------------------------------------

def check_name(name):
    """Der Name, unter dem eine Datei in sciebo liegen soll. Fehler als Text, sonst None.

    Geprüft wird, dass der Name ein einzelnes Pfadsegment ist und nicht in den
    Namensraum greift, den dieses Skript für sich reserviert (führender Punkt).
    Über den Inhalt des Namens entscheidet der Aufrufer:
    jede Endung ist erlaubt, auch mehrteilige wie .tar.gz, und ob eine Version
    darin steht, geht dieses Skript nichts an.
    """
    if not isinstance(name, str) or not name:
        return "leerer Name"
    if len(name.encode("utf-8")) > MAX_NAME:
        return f"länger als {MAX_NAME} Byte"
    if name in (".", ".."):
        return "'.' und '..' sind keine Dateinamen"
    if "/" in name or "\\" in name:
        return "enthält einen Pfadtrenner"
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in name):
        return "enthält ein Steuerzeichen"
    if name != name.strip():
        return "beginnt oder endet mit Leerraum"
    if name.startswith("."):
        return f"beginnt mit einem Punkt - der Namensraum ist für {UPLOAD_PREFIX} reserviert"
    if unicodedata.normalize("NFC", name) != name:
        return "ist nicht NFC-normalisiert - sonst hängt der Name vom Client ab"
    return None


def upload_name(name):
    """Der versteckte Zwischenname, auf den hochgeladen wird."""
    return UPLOAD_PREFIX + "." + name


def is_upload_name(name):
    return name.startswith(UPLOAD_PREFIX + ".")


# --- Umgebung und lokale Dateien --------------------------------------------

def dav_root(base, user):
    """WebDAV-Wurzel des Kontos aus SCIEBO_BASE_URL und SCIEBO_USER.

    Kurzform https://<host>: /remote.php/dav/files/<login> wird angehängt, der
    Login dabei prozent-kodiert. Langform mit /remote.php/dav/files/<login>
    bleibt unverändert, falls die interne Nextcloud-Kennung einmal nicht der
    Login ist. Alles andere unter /remote.php/ ist ein Irrtum (z.B.
    remote.php/webdav) und wird abgelehnt.
    """
    b = base.rstrip("/")
    if DAV_FILES in b:
        return b
    if "/remote.php" in b:
        raise ValueError("SCIEBO_BASE_URL: entweder nur https://<host> oder die volle "
                         "Dateien-Wurzel https://<host>/remote.php/dav/files/<login>")
    if not user or "/" in user or user in (".", ".."):
        raise ValueError("SCIEBO_USER taugt nicht als Pfadsegment")
    return b + DAV_FILES + urllib.parse.quote(user, safe="")


def check_folder(folder):
    """SCIEBO_FOLDER als Literal prüfen. Fehler als Text, sonst None.

    Das '%' ist der Grund, warum diese Prüfung existiert: der Zielordner wird
    unten kodiert, und ein bereits im Wert stehendes Prozentzeichen lässt offen,
    ob "2025%20alt" den Ordner "2025%20alt" oder "2025 alt" meint. Ein Lauf, der
    einen anderen Ordner adressiert als konfiguriert, fällt niemandem auf.
    """
    if not folder:
        return "leer - das hieße, in die Kontowurzel zu veröffentlichen"
    if "%" in folder:
        return "enthält '%' - der Ordnername wird kodiert, bitte das Zeichen vermeiden"
    if "\\" in folder:
        return "enthält '\\' - Pfadtrenner ist '/'"
    for seg in folder.split("/"):
        if seg.strip() in ("", ".", ".."):
            return "leeres Segment, '.' oder '..' im Pfad"
    return None


def join_target(root, folder):
    """Dateien-Wurzel und Zielordner zur Ziel-URL. Nur der Ordner wird kodiert;
    die Wurzel behält die Kodierung, mit der sie hereinkam."""
    return root.rstrip("/") + "/" + urllib.parse.quote(folder.strip("/"), safe="/")


def read_env():
    # Leer zählt als fehlend: eine nicht gesetzte GitHub-Variable expandiert zu "",
    # und ein leerer Ordner hieße "in die Kontowurzel veröffentlichen".
    missing = [v for v in ENV_VARS if not os.environ.get(v, "").strip()]
    if missing:
        die("Nicht gesetzt (leer zählt als fehlend): " + ", ".join(missing))
    base = os.environ["SCIEBO_BASE_URL"].strip()
    if not base.lower().startswith("https://"):
        die("SCIEBO_BASE_URL muss mit https:// beginnen (Basic Auth über http würde das Passwort offenlegen)")
    folder = os.environ["SCIEBO_FOLDER"].strip().strip("/")
    problem = check_folder(folder)
    if problem:
        die(f"SCIEBO_FOLDER ungültig: {problem}")
    user = os.environ["SCIEBO_USER"]
    try:
        root = dav_root(base, user)
    except ValueError as exc:
        die(str(exc))
    return root, folder, user, os.environ["SCIEBO_APP_PASSWORD"]


def collect_local(dist_dir):
    """Alle regulären Dateien direkt in DIST_DIR, mit Bytes und Prüfsumme.

    Unterverzeichnisse werden nicht betreten: dieser Spiegel kennt nur Dateien,
    flach. Wer einen Ordner veröffentlichen will, packt ihn vorher.
    """
    if not os.path.isdir(dist_dir):
        die(f"Verzeichnis nicht gefunden: {dist_dir}")
    names = sorted(os.listdir(dist_dir))
    if not names:
        die(f"{dist_dir} ist leer - nichts zu veröffentlichen")
    files = []
    for name in names:
        path = os.path.join(dist_dir, name)
        if os.path.isdir(path):
            die(f"{name} ist ein Verzeichnis - dieser Spiegel kennt nur Dateien, flach")
        if not os.path.isfile(path):
            die(f"{name} ist keine reguläre Datei")
        problem = check_name(name)
        if problem:
            die(f"{name}: unbrauchbarer Dateiname ({problem})")
        with open(path, "rb") as fh:
            data = fh.read()
        if not data:
            die(f"{name} ist leer")
        files.append({"name": name, "data": data, "size": len(data),
                      "sha256": hashlib.sha256(data).hexdigest()})
    return files


# --- WebDAV -----------------------------------------------------------------

def parse_multistatus(payload, base_path):
    """207-Body -> direkte Kinder des Zielordners: [{"name", "is_collection"}].

    Hrefs kommen prozent-kodiert und server-absolut (manchmal als volle URL);
    beides wird dekodiert und gegen den dekodierten Basis-Pfad verglichen.
    Wirft ValueError, wenn das XML unlesbar ist oder keine Antwort zum Basis-Pfad
    passt - dann stimmen Basis-URL und Server-Sicht nicht überein.
    """
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise ValueError(f"Multistatus-XML unlesbar ({exc})")
    parent = urllib.parse.unquote(base_path).rstrip("/")
    entries, responses, self_seen = [], 0, False
    for resp in root.iter("{DAV:}response"):
        responses += 1
        href = resp.findtext("{DAV:}href") or ""
        path = urllib.parse.unquote(urllib.parse.urlsplit(href).path).rstrip("/")
        if path == parent:
            self_seen = True
            continue
        if not path.startswith(parent + "/"):
            continue
        rest = path[len(parent) + 1:]
        if not rest or "/" in rest:
            continue  # nur direkte Kinder
        is_coll = resp.find(".//{DAV:}resourcetype/{DAV:}collection") is not None
        entries.append({"name": rest, "is_collection": is_coll})
    if responses and not self_seen:
        raise ValueError("PROPFIND-Antwort passt nicht zur Basis-URL - die Dateien-Wurzel muss "
                         "https://<host>/remote.php/dav/files/<login> sein, nicht remote.php/webdav")
    return sorted(entries, key=lambda e: e["name"])


class Dav:
    RETRY_CODES = (423, 429, 500, 502, 503, 504)

    def __init__(self, target, user, password):
        self.base = target.rstrip("/")
        self.base_path = urllib.parse.urlsplit(self.base).path
        token = base64.b64encode(f"{user}:{password}".encode()).decode()
        self.auth = "Basic " + token

    def url_for(self, name):
        return self.base + "/" + urllib.parse.quote(name, safe="")

    def request(self, method, name="", data=None, headers=None, timeout=120):
        """(status, body). 0 = kein Ergebnis nach 3 Versuchen. Wiederholt Netzfehler,
        Locks (423), Drosselung (429) und 5xx mit Pausen 2 s, 4 s.
        Meldungen enthalten nie die URL - und damit nie den Login."""
        # Der Guard steht VOR dem Socket, nicht daneben: kein DELETE, kein MKCOL,
        # nichts außer den vier Methoden, die dieser Spiegel braucht. Bewusst kein
        # `assert`, das fällt unter `python3 -O` weg.
        if method not in ALLOWED_METHODS:
            raise RuntimeError(
                f"Schutz ausgelöst: Methode {method!r} ist in diesem Skript nicht vorgesehen "
                f"(erlaubt: {', '.join(ALLOWED_METHODS)}) - es wird nie gelöscht und nie ein Ordner angelegt")
        url = self.base + ("/" + urllib.parse.quote(name, safe="") if name else "")
        for attempt in range(3):
            req = urllib.request.Request(url, data=data, method=method)
            req.add_header("Authorization", self.auth)
            req.add_header("User-Agent", "ent-publish-sciebo/3")
            for k, v in (headers or {}).items():
                req.add_header(k, v)
            if data is not None and "Content-Type" not in (headers or {}):
                req.add_header("Content-Type", "application/octet-stream")
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    return resp.status, resp.read()
            except urllib.error.HTTPError as exc:
                if exc.code in self.RETRY_CODES and attempt < 2:
                    time.sleep(2 * (attempt + 1))
                    continue
                return exc.code, b""
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                log(f"  ! {method} {name or '(Zielordner)'}: {type(exc).__name__} (Versuch {attempt + 1}/3)")
                if attempt < 2:
                    time.sleep(2 * (attempt + 1))
        return 0, b""

    def listing(self):
        """PROPFIND Depth 1. (status, entries) - entries None, wenn nicht 207 oder unlesbar."""
        body = ('<?xml version="1.0" encoding="utf-8"?><d:propfind xmlns:d="DAV:">'
                '<d:prop><d:resourcetype/></d:prop></d:propfind>').encode()
        status, payload = self.request("PROPFIND", data=body, timeout=60,
                                       headers={"Depth": "1", "Content-Type": "application/xml; charset=utf-8"})
        if status != 207:
            return status, None
        try:
            return status, parse_multistatus(payload, self.base_path)
        except ValueError as exc:
            warn(str(exc), "PROPFIND unlesbar")
            return status, None

    def replace(self, f):
        """Eine Datei ersetzen: PUT auf den Zwischennamen, zurücklesen, MOVE auf das Ziel.

        Gibt (True, "") zurück oder (False, Grund). Bei jedem Fehlschlag bleibt
        die bisherige Datei unter dem Zielnamen unberührt.
        """
        name, tmp = f["name"], upload_name(f["name"])
        status, _ = self.request("PUT", tmp, data=f["data"])
        if status not in (200, 201, 204):
            return False, f"Upload fehlgeschlagen ({explain(status)})"
        status, body = self.request("GET", tmp)
        if status != 200:
            return False, f"Nachprüfung fehlgeschlagen ({explain(status)})"
        if hashlib.sha256(body).hexdigest() != f["sha256"]:
            return False, "Nachprüfung fehlgeschlagen (Inhalt weicht ab)"
        status, _ = self.request("MOVE", tmp, timeout=60,
                                 headers={"Destination": self.url_for(name), "Overwrite": "T"})
        if status not in (201, 204):
            return False, f"Umbenennen fehlgeschlagen ({explain(status)})"
        return True, ""


def explain(code):
    return {
        0: "Netzwerkfehler - keine Antwort nach 3 Versuchen",
        401: "Zugangsdaten abgelehnt - SCIEBO_USER / App-Passwort prüfen",
        403: "Zugriff verweigert - Rechte auf den Ordner oder App-Passwort prüfen",
        404: "Zielordner fehlt - SCIEBO_FOLDER prüfen (der Ordner wird absichtlich nicht angelegt)",
        409: "übergeordneter Pfad fehlt - SCIEBO_FOLDER prüfen",
        423: "Datei ist gesperrt (Lock) - später erneut versuchen",
        507: "sciebo-Speicher voll",
    }.get(code, f"HTTP {code}")


# --- Ablauf -------------------------------------------------------------------

def write_summary(folder, rows):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    lines = ["### sciebo", "", f"Ordner: `{folder}`", "",
             "| Datei | Aktion | Größe | SHA-256 |", "|---|---|---|---|"]
    for name, action, size, sha in rows:
        size_s = f"{size / 1024:.0f} KB" if size else "-"
        sha_s = f"`{sha[:16]}`" if sha else "-"
        lines.append(f"| `{name}` | {action} | {size_s} | {sha_s} |")
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n\n")


def report_folder(entries, planned):
    """Den Ordnerinhalt zeigen, und sonst nichts.

    Markiert wird nur, was dieser Lauf schreiben wollte. Über alles andere sagt
    dieser Bericht bewusst nichts aus: mehrere Repos teilen sich einen Ordner,
    und ein Lauf kann die aktuelle Datei eines anderen Repos nicht von einer
    eigenen Karteileiche unterscheiden. Diese Unterscheidung ist genau das, was
    dieser Spiegel nicht mehr trifft - also darf er sie auch nicht andeuten.
    """
    for e in entries:
        name = e["name"]
        if e["is_collection"]:
            kind = "ordner "
        elif name in planned:
            kind = "dieser "
        elif is_upload_name(name):
            kind = "rest   "  # abgebrochener Upload, wird beim nächsten Lauf überschrieben
        else:
            kind = "       "
        log(f"  [{kind}] {name}")


def main(argv):
    if "--self-test" in argv:
        return self_test()
    dry_run = "--dry-run" in argv
    rest = [a for a in argv if a != "--dry-run"]
    if any(a.startswith("-") for a in rest) or len(rest) > 1:
        die("Aufruf: publish_sciebo.py [--dry-run] [DIST_DIR] | --self-test")
    dist_dir = rest[0] if rest else None
    if not dry_run and not dist_dir:
        die("DIST_DIR fehlt - ohne --dry-run ist das Verzeichnis mit den Dateien Pflicht")

    root, folder, user, password = read_env()
    target = join_target(root, folder)
    dav = Dav(target, user, password)
    if IN_CI:
        # Der Login ist ein Secret; seine kodierte Form (%40 statt @) weicht davon ab
        # und würde sonst nicht maskiert. Die ::add-mask::-Zeilen selbst erscheinen nicht im Log.
        log("::add-mask::" + urllib.parse.quote(user, safe=""))
        log("::add-mask::" + dav.auth)
    log(f"Ziel: {target}")
    log("Es wird ausschließlich hochgeladen und überschrieben. Kein DELETE, kein MKCOL.")
    if dry_run:
        log("DRY RUN - es wird nichts geschrieben")

    local = collect_local(dist_dir) if dist_dir else []
    for f in local:
        log(f"  bereit: {f['name']}  {f['size'] / 1024:.0f} KB  sha256 {f['sha256'][:16]}")

    # Vorher-Bild, zugleich die Existenzprüfung des Zielordners.
    status, before = dav.listing()
    if before is None:
        die(f"Zielordner nicht lesbar: {explain(status) if status != 207 else 'Antwort unlesbar'}")
    before_names = {e["name"] for e in before}
    log(f"Ordner vorher ({len(before)} Einträge):")
    report_folder(before, {f["name"] for f in local})

    if dry_run:
        planned = sorted(f["name"] for f in local)
        log("Würde schreiben: " + (", ".join(planned) or "nichts (kein DIST_DIR)"))
        log("Würde löschen:  nichts, nie.")
        log("DRY RUN beendet - nichts verändert.")
        return 0

    rows, ok, failed = [], [], []
    for f in local:
        good, why = dav.replace(f)
        if good:
            verb = "ersetzt" if f["name"] in before_names else "neu"
            log(f"  OK  {f['name']} -> {verb}, byte-identisch bestätigt")
            ok.append(f)
            rows.append((f["name"], f"hochgeladen ({verb}), byte-identisch bestätigt", f["size"], f["sha256"]))
        else:
            log(f"  x   {f['name']}: {why}")
            failed.append(f)
            rows.append((f["name"], why, f["size"], f["sha256"]))

    planned = {f["name"] for f in local}
    status, after = dav.listing()
    if after is None:
        warn("Ordnerliste nach dem Upload nicht lesbar - der Bericht bleibt unvollständig. "
             "Geschrieben wurde trotzdem, gelöscht wird ohnehin nie.", "PROPFIND fehlgeschlagen")
    else:
        log(f"Ordner nachher ({len(after)} Einträge):")
        report_folder(after, planned)
        missing = [f["name"] for f in ok if f["name"] not in {e["name"] for e in after}]
        for name in missing:
            warn(f"{name} fehlt in der Ordnerliste nach dem Upload.", "Nachprüfung")

    write_summary(folder, rows)

    log("---")
    if failed:
        die(f"FEHLGESCHLAGEN: {len(failed)} Datei(en) nicht ersetzt. Die bisherigen Dateien "
            "stehen unverändert im Ordner. Job erneut ausführen.")
    log(f"OK: {len(ok)} Datei(en) geschrieben und bestätigt, 0 gelöscht.")
    return 0


# --- Selbsttest (offline) -------------------------------------------------------

def self_test():
    checks = 0

    def ok(cond, what):
        nonlocal checks
        checks += 1
        if not cond:
            die(f"Selbsttest fehlgeschlagen: {what}")

    # 1. Die zentrale Zusage: keine andere Methode als die vier erlaubten, und der
    #    Guard greift VOR dem Socket. Bewiesen mit einem urlopen, das jeden Aufruf
    #    als Fehler meldet.
    dav = Dav("https://example.invalid/remote.php/dav/files/u/x", "u", "p")
    real_urlopen = urllib.request.urlopen

    def tripwire(*_a, **_kw):
        raise AssertionError("Socket geöffnet, obwohl der Guard hätte greifen müssen")

    urllib.request.urlopen = tripwire
    try:
        for method in ("DELETE", "MKCOL", "PROPPATCH", "POST", "delete", "put"):
            try:
                dav.request(method, "egal")
                ok(False, f"Methode {method!r} wurde nicht abgewiesen")
            except AssertionError:
                raise
            except RuntimeError:
                ok(True, "")
    finally:
        urllib.request.urlopen = real_urlopen
    ok(ALLOWED_METHODS == ("PUT", "GET", "PROPFIND", "MOVE"), "Methodenliste unverändert")
    ok("DELETE" not in ALLOWED_METHODS and "MKCOL" not in ALLOWED_METHODS, "DELETE/MKCOL nicht erlaubt")

    # 2. Dateinamen
    for name in ("sciebo-latest.mcpb", "uni-mail-exchange-latest.mcpb", "ent-slides.plugin",
                 "skills-pack.tar.gz", "Handreichung Extensions.pdf", "muenster.md",
                 "a" * MAX_NAME):
        ok(check_name(name) is None, f"check_name({name!r}) hätte durchgehen müssen")
    for name, why in [("", "leer"), (".", "punkt"), ("..", "punktpunkt"),
                      ("a/b.mcpb", "slash"), ("a\\b.mcpb", "backslash"),
                      (".versteckt.mcpb", "führender punkt"),
                      (".upload.x.mcpb", "reservierter zwischenname"),
                      ("x.mcpb\n", "steuerzeichen"), (" x.mcpb", "leerraum vorn"),
                      ("x.mcpb ", "leerraum hinten"), ("a" * (MAX_NAME + 1), "zu lang"),
                      (unicodedata.normalize("NFD", "münster.mcpb"), "nicht NFC")]:
        ok(check_name(name) is not None, f"check_name({name!r}) hätte scheitern müssen ({why})")
    ok(upload_name("sciebo-latest.mcpb") == ".upload.sciebo-latest.mcpb", "upload_name")
    ok(check_name(upload_name("x.mcpb")) is not None,
       "der eigene Zwischenname ist kein gültiger Quellname")
    ok(upload_name("x.mcpb").endswith(".mcpb"),
       "der Zwischenname endet auf dieselbe Endung wie sein Ziel - sonst greift Nextclouds Endungssperre")
    ok(is_upload_name(".upload.x.mcpb") and not is_upload_name("x.mcpb"), "is_upload_name")

    # 3. Zielordner, insbesondere das Prozentzeichen
    for folder in ("Desktop Extensions", "21 Claude/Marketplace Basic", "a/b/c"):
        ok(check_folder(folder) is None, f"check_folder({folder!r}) hätte durchgehen müssen")
    for folder, why in [("", "leer"), ("2025%20alt", "prozent"), ("a//b", "leeres segment"),
                        ("a/../b", "punktpunkt"), ("a/./b", "punkt"), ("a\\b", "backslash")]:
        ok(check_folder(folder) is not None, f"check_folder({folder!r}) hätte scheitern müssen ({why})")

    # 4. URL-Aufbau
    ok(dav_root("https://h", "u@x.de") == "https://h/remote.php/dav/files/u%40x.de", "dav_root kurz")
    ok(dav_root("https://h/", "u@x.de") == "https://h/remote.php/dav/files/u%40x.de", "dav_root kurz mit Slash")
    ok(dav_root("https://h/remote.php/dav/files/u%40x.de/", "u@x.de") ==
       "https://h/remote.php/dav/files/u%40x.de", "dav_root lang unverändert")
    ok(dav_root("https://h/remote.php/dav/files/andere-kennung", "u@x.de") ==
       "https://h/remote.php/dav/files/andere-kennung", "dav_root lang gewinnt über SCIEBO_USER")
    for bad_base, bad_user, what in [("https://h/remote.php/webdav", "u", "remote.php/webdav"),
                                     ("https://h/remote.php/dav", "u", "remote.php/dav ohne files"),
                                     ("https://h", "", "leerer Login"),
                                     ("https://h", "a/b", "Login mit Slash")]:
        try:
            dav_root(bad_base, bad_user)
            ok(False, f"dav_root hat {what} akzeptiert")
        except ValueError:
            ok(True, "")
    ok(join_target("https://h/remote.php/dav/files/u%40x.de", "/A B/C/") ==
       "https://h/remote.php/dav/files/u%40x.de/A%20B/C", "join_target kodiert den Ordner")
    ok(join_target("https://h/remote.php/dav/files/u%40x.de", "A B/C") ==
       join_target("https://h/remote.php/dav/files/u%40x.de/", "/A B/C/"), "join_target ist robust gegen Slashes")
    d = Dav(join_target("https://h/remote.php/dav/files/u%40x.de", "A B"), "u", "p")
    ok(d.url_for("x y.mcpb") == "https://h/remote.php/dav/files/u%40x.de/A%20B/x%20y.mcpb", "url_for")
    ok(d.url_for(upload_name("x.mcpb")) ==
       "https://h/remote.php/dav/files/u%40x.de/A%20B/.upload.x.mcpb", "url_for Zwischenname")

    # 5. Ordnerliste
    xml = (b'<?xml version="1.0"?><d:multistatus xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">'
           b'<d:response><d:href>/remote.php/dav/files/u%40x.de/A%20B/C/</d:href>'
           b'<d:propstat><d:prop><d:resourcetype><d:collection/></d:resourcetype></d:prop>'
           b'<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>'
           b'<d:response><d:href>/remote.php/dav/files/u%40x.de/A%20B/C/sciebo-latest.mcpb</d:href>'
           b'<d:propstat><d:prop><d:resourcetype/></d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat>'
           b'<d:propstat><d:prop><oc:foo/></d:prop><d:status>HTTP/1.1 404 Not Found</d:status></d:propstat></d:response>'
           b'<d:response><d:href>https://h/remote.php/dav/files/u%40x.de/A%20B/C/Sub%20Folder/</d:href>'
           b'<d:propstat><d:prop><d:resourcetype><d:collection/></d:resourcetype></d:prop>'
           b'<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>'
           b'<d:response><d:href>/remote.php/dav/files/u%40x.de/A%20B/C/Sub%20Folder/deep.mcpb</d:href>'
           b'<d:propstat><d:prop><d:resourcetype/></d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>'
           b'<d:response><d:href>/remote.php/dav/files/u%40x.de/A%20B/C/.version.json</d:href>'
           b'<d:propstat><d:prop><d:resourcetype/></d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>'
           b'</d:multistatus>')
    entries = parse_multistatus(xml, "/remote.php/dav/files/u%40x.de/A%20B/C")
    ok([(e["name"], e["is_collection"]) for e in entries] ==
       [(".version.json", False), ("Sub Folder", True), ("sciebo-latest.mcpb", False)],
       f"parse_multistatus: {entries}")
    for bad_payload, bad_base, what in [(b"<not xml", "/x", "kaputtes XML"),
                                        (xml, "/remote.php/webdav/A%20B/C", "falsche Basis")]:
        try:
            parse_multistatus(bad_payload, bad_base)
            ok(False, f"parse_multistatus hat {what} akzeptiert")
        except ValueError:
            ok(True, "")

    # 6. Der Bericht listet nur auf und bewertet nichts. Mehrere Repos teilen sich
    #    einen Ordner: die aktuelle Datei eines anderen Repos sieht von hier aus
    #    genauso aus wie eine eigene Karteileiche, also wird über keine von beiden
    #    etwas behauptet.
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        result = report_folder(entries, {"sciebo-latest.mcpb"})
    shown = buf.getvalue()
    ok(result is None, "report_folder gibt keine Bewertung zurück")
    for name in (".version.json", "Sub Folder", "sciebo-latest.mcpb"):
        ok(name in shown, f"{name} fehlt in der Auflistung")
    ok("dieser" in shown, "die Dateien dieses Laufs sind erkennbar")
    for wort in ("fremd", "entfernt", "entfernen", "veraltet", "Karteileiche"):
        ok(wort not in shown, f"der Bericht urteilt mit dem Wort {wort!r} über fremde Dateien")

    log(f"Selbsttest OK: {checks} Prüfungen bestanden")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
