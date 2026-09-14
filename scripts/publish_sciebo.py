#!/usr/bin/env python3
"""publish_sciebo.py - EINE Extension bzw. EIN Plugin nach sciebo, sonst nichts.

Lädt die gerade gebauten Bundles eines Slugs in den sciebo-Ordner und entfernt
danach die überholten Dateien DESSELBEN Slugs. Alles andere im Ordner gehört
anderen Repos und wird nie angefasst. Es gibt keine Zustandsdatei mehr: die
Ordnerliste ist der Zustand.

Zwei Modi, ein Ablauf:
  versioned  Die Version steht im Dateinamen (<slug>[-<variante>]-v<x.y.z>.<ext>),
             jeder Lauf legt neue Dateien an und räumt die Vorgänger weg.
  fixed      Ein fester Dateiname ohne Version (<slug>.<ext>), jeder Lauf
             überschreibt ihn. Ein stabiler Link bleibt damit gültig.

Eigentum = Dateiname endet auf ".<ext>", beginnt mit "<slug>-" - im Modus fixed
zusätzlich der blosse Name "<slug>.<ext>" - und beginnt NICHT mit
"<anderer-slug>-" für einen der übrigen bekannten Slugs. Nur das.

Aufruf:
  publish_sciebo.py DIST_DIR              DIST_DIR/*.<ext> hochladen, dann aufräumen
  publish_sciebo.py --dry-run [DIST_DIR]  nur PROPFIND; zeigt, was passieren würde
  publish_sciebo.py --self-test           Offline-Prüfung der Schutzlogik, kein Netz

Umgebung:
  SLUG                 Kurzname der Extension bzw. des Plugins, z.B. sciebo oder ent-thesis
  MODE                 versioned (Standard) oder fixed
  EXT                  Dateiendung ohne Punkt: mcpb (Standard) oder plugin
  OTHER_SLUGS          die übrigen bekannten Slugs, durch Leerzeichen getrennt
                       (optional; kein Slug darf Präfix eines anderen sein)
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
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile

IN_CI = os.environ.get("GITHUB_ACTIONS") == "true"
ENV_VARS = ("SLUG", "SCIEBO_BASE_URL", "SCIEBO_FOLDER", "SCIEBO_USER", "SCIEBO_APP_PASSWORD")
SLUG_RE = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")
MODES = ("versioned", "fixed")
EXTS = ("mcpb", "plugin")
# Wo im ZIP das Manifest liegt, aus dem geprüft wird, dass das Bundle wirklich
# zu diesem Slug gehört.
MANIFEST_IN_BUNDLE = {"mcpb": "manifest.json", "plugin": ".claude-plugin/plugin.json"}
# <slug>[-<variante>]-v<major>.<minor>.<patch>[-<prerelease>].<ext>
# Prerelease nur mit Bindestrich (SemVer), damit "x-v1.0.0.mcpb.mcpb" nicht passt.
BUNDLE_RE_TEMPLATE = (r"{slug}(?P<variant>-[a-z0-9]+)?-v(?P<version>[0-9]+\.[0-9]+\.[0-9]+"
                      r"(-[0-9A-Za-z][0-9A-Za-z.-]*)?)\.{ext}")
OTHER_SLUGS = ()  # wird in read_env() gesetzt; owns_file() nimmt alternativ `others`
MODE = "versioned"  # wird in read_env() gesetzt; owns_file()/family_of() nehmen alternativ `mode`
EXT = "mcpb"        # wird in read_env() gesetzt; die Funktionen nehmen alternativ `ext`


def log(msg):
    print(msg, flush=True)


def warn(msg, title="publish-sciebo"):
    log(f"::warning title={title}::{msg}" if IN_CI else f"WARNUNG: {msg}")


def die(msg, code=1):
    log(f"::error::{msg}" if IN_CI else f"FEHLER: {msg}")
    sys.exit(code)


# --- Eigentum: die eine Funktion, die entscheidet ---------------------------

def resolve_mode_ext(mode, ext):
    """Modus und Endung auflösen (None = Globals) und gegen die Listen prüfen.

    Ein unbekannter Modus oder eine unbekannte Endung ist ein Programmierfehler
    und wirft - genau wie ein ungültiger Slug. Stillschweigend auf den Standard
    zurückzufallen hiesse, im falschen Namensraum aufzuräumen.
    """
    mode = MODE if mode is None else mode
    ext = EXT if ext is None else ext
    if mode not in MODES:
        raise ValueError(f"ungültiger Modus: {mode!r} - erlaubt: {', '.join(MODES)}")
    if ext not in EXTS:
        raise ValueError(f"ungültige Endung: {ext!r} - erlaubt: {', '.join(EXTS)}")
    return mode, ext


def owns_file(slug, name, others=None, mode=None, ext=None):
    """True genau dann, wenn NAME eine Datei dieses Slugs ist.

    Präfix "<slug>-" und Suffix ".<ext>", sonst nichts. Absichtlich kein Regex auf
    die Version: auch ein von Hand abgelegtes "uni-mail-test.mcpb" gehört uni-mail
    und darf von uni-mail weggeräumt werden - "uni-mailer-v1.0.0.mcpb" aber nie,
    und "VERSIONS.md" oder ".version.json" erst recht nicht.

    Im Modus fixed gehört zusätzlich der blosse Name "<slug>.<ext>" dazu: dort
    trägt der Dateiname keine Version. Das Präfix "<slug>-" bleibt auch dort
    eigen, damit ein Lauf die versionierten Altlasten desselben Slugs aufräumen
    kann, statt sie liegen zu lassen.

    Beginnt der Name mit "<other>-" für einen anderen bekannten Slug, gehört er
    nie diesem Slug - auch dann nicht, wenn er zugleich mit "<slug>-" beginnt.
    """
    if not isinstance(slug, str) or not SLUG_RE.fullmatch(slug):
        raise ValueError(f"ungültiger Slug: {slug!r}")
    mode, ext = resolve_mode_ext(mode, ext)
    others = OTHER_SLUGS if others is None else tuple(others)
    if not isinstance(name, str) or not name:
        return False
    if "/" in name or "\\" in name or name in (".", ".."):
        return False
    if not name.endswith("." + ext):
        return False
    if not (name.startswith(slug + "-") or (mode == "fixed" and name == f"{slug}.{ext}")):
        return False
    for other in others:
        if other != slug and name.startswith(other + "-"):
            return False
    return True


def guard_owned(slug, name, ext=None):
    """Re-Assertion unmittelbar vor jedem DELETE: wirft, statt zu löschen.

    Absichtlich kein `assert` - das fällt unter `python3 -O` weg.
    """
    _, ext = resolve_mode_ext(None, ext)
    if not owns_file(slug, name, ext=ext):
        raise RuntimeError(f"Schutz ausgelöst: {name!r} gehört nicht zu {slug!r} - kein DELETE")
    if not name.endswith("." + ext):
        raise RuntimeError(f"Schutz ausgelöst: {name!r} ist keine .{ext} - kein DELETE")
    return True


def bundle_re(slug, ext=None):
    ext = EXT if ext is None else ext
    return re.compile(BUNDLE_RE_TEMPLATE.format(slug=re.escape(slug), ext=re.escape(ext)))


def family_of(slug, name, mode=None, ext=None):
    """'uni-mail-exchange-v1.1.1.mcpb' -> 'uni-mail-exchange'; ohne Schema -> None.

    Im Modus fixed gibt es nur einen Dateinamen je Slug, der Namensraum ist also
    genau eine Familie: alles Eigene gehört zu 'slug', alles andere zu keiner.
    """
    mode, ext = resolve_mode_ext(mode, ext)
    if mode == "fixed":
        return slug if owns_file(slug, name, mode=mode, ext=ext) else None
    m = bundle_re(slug, ext).fullmatch(name)
    if not m:
        return None
    return slug + (m.group("variant") or "")


def check_slug_family(slug, others):
    """Namensregel: kein bekannter Slug darf Präfix eines anderen sein (beide
    Richtungen). Sonst räumte der eine die Dateien des anderen weg."""
    for other in others:
        if not SLUG_RE.fullmatch(other):
            die(f"OTHER_SLUGS enthält ungültigen Slug {other!r}")
        if other == slug:
            die(f"OTHER_SLUGS enthält den eigenen Slug {slug!r}")
        if other.startswith(slug + "-") or slug.startswith(other + "-"):
            die(f"Namensregel verletzt: {slug!r} und {other!r} - einer ist Präfix des anderen. "
                "So könnte ein Repo die Bundles des anderen löschen. Abbruch.")


# --- Umgebung und lokale Bundles ---------------------------------------------

DAV_FILES = "/remote.php/dav/files/"


def dav_root(base, user):
    """WebDAV-Wurzel des Kontos aus SCIEBO_BASE_URL und SCIEBO_USER.

    Kurzform https://<host>: /remote.php/dav/files/<login> wird angehängt, damit
    der Login nur einmal (in SCIEBO_USER) gepflegt wird. Langform mit
    /remote.php/dav/files/<login> bleibt unverändert - falls die interne
    Nextcloud-Kennung einmal nicht der Login ist. Alles andere unter
    /remote.php/ ist ein Irrtum (z.B. remote.php/webdav) und wird abgelehnt.
    """
    b = base.rstrip("/")
    if DAV_FILES in b:
        return b
    if "/remote.php" in b:
        raise ValueError("SCIEBO_BASE_URL: entweder nur https://<host> oder die volle "
                         "Dateien-Wurzel https://<host>/remote.php/dav/files/<login>")
    if not user or "/" in user or user in (".", ".."):
        raise ValueError("SCIEBO_USER taugt nicht als Pfadsegment")
    return b + DAV_FILES + user


def read_env():
    global OTHER_SLUGS, MODE, EXT
    # Leer zählt als fehlend: eine nicht gesetzte GitHub-Variable expandiert zu "",
    # und ein leerer Ordner hieße "in die Kontowurzel veröffentlichen".
    missing = [v for v in ENV_VARS if not os.environ.get(v, "").strip()]
    if missing:
        die("Nicht gesetzt (leer zählt als fehlend): " + ", ".join(missing))
    slug = os.environ["SLUG"].strip()
    if not SLUG_RE.fullmatch(slug):
        die(f"SLUG {slug!r} ungültig - erlaubt: Kleinbuchstaben, Ziffern, einzelne Bindestriche")
    # Modus und Endung zuerst: sie bestimmen, was dieser Lauf als eigen ansieht.
    mode = os.environ.get("MODE", "").strip() or MODES[0]
    if mode not in MODES:
        die(f"MODE {mode!r} ungültig - erlaubt: {', '.join(MODES)}")
    ext = os.environ.get("EXT", "").strip() or EXTS[0]
    if ext.startswith("."):
        ext = ext[1:]
    if ext not in EXTS:
        die(f"EXT {ext!r} ungültig - erlaubt: {', '.join(EXTS)}")
    MODE, EXT = mode, ext
    others = tuple(os.environ.get("OTHER_SLUGS", "").split())
    check_slug_family(slug, others)
    OTHER_SLUGS = others
    base = os.environ["SCIEBO_BASE_URL"].strip()
    if not base.lower().startswith("https://"):
        die("SCIEBO_BASE_URL muss mit https:// beginnen (Basic Auth über http würde das Passwort offenlegen)")
    folder = os.environ["SCIEBO_FOLDER"].strip().strip("/")
    if not folder or any(seg.strip() in ("", ".", "..") for seg in folder.split("/")):
        die("SCIEBO_FOLDER ungültig - leer, doppelte Schrägstriche oder '.'/'..'-Segmente")
    user = os.environ["SCIEBO_USER"]
    try:
        root = dav_root(base, user)
    except ValueError as exc:
        die(str(exc))
    return slug, root, folder, user, os.environ["SCIEBO_APP_PASSWORD"]


def read_manifest(path, ext):
    """Manifest aus dem Bundle lesen: manifest.json (.mcpb), .claude-plugin/plugin.json (.plugin).

    Wirft zipfile.BadZipFile (kein ZIP), KeyError (Manifest fehlt) oder ValueError
    (kein lesbares JSON) - der Aufrufer bricht darauf ab.
    """
    with zipfile.ZipFile(path) as z:
        return json.loads(z.read(MANIFEST_IN_BUNDLE[ext]))


def collect_local(dist_dir, slug):
    """Alle DIST_DIR/*.<ext> - jede muss zum Slug und zum Namensschema des Modus
    passen, sonst Abbruch, bevor sciebo berührt wird (HARD GUARD 1)."""
    if not os.path.isdir(dist_dir):
        die(f"Verzeichnis nicht gefunden: {dist_dir}")
    names = sorted(n for n in os.listdir(dist_dir) if n.endswith("." + EXT))
    if not names:
        die(f"Keine .{EXT} in {dist_dir} - nichts zu veröffentlichen")
    if MODE == "fixed" and len(names) > 1:
        die(f"Modus fixed: genau eine .{EXT} erwartet, gefunden {len(names)} ({', '.join(names)})")
    pattern = bundle_re(slug)
    files = []
    for name in names:
        path = os.path.join(dist_dir, name)
        if not os.path.isfile(path):
            die(f"{name} ist keine reguläre Datei")
        m = None
        if MODE == "fixed":
            if name != f"{slug}.{EXT}" or not owns_file(slug, name):
                die(f"{name}: im Modus fixed ist genau {slug}.{EXT} erlaubt - "
                    "Abbruch, bevor sciebo berührt wird")
        else:
            m = pattern.fullmatch(name)
            if not owns_file(slug, name) or not m:
                die(f"{name}: passt nicht zum Schema {slug}[-variante]-v<major>.<minor>.<patch>.{EXT} "
                    f"für Slug {slug!r} - Abbruch, bevor sciebo berührt wird")
        with open(path, "rb") as fh:
            data = fh.read()
        if not data:
            die(f"{name} ist leer")
        try:
            manifest = read_manifest(path, EXT)
        except (zipfile.BadZipFile, KeyError, ValueError) as exc:
            die(f"{name}: kein lesbares Bundle ({exc})")
        if MODE == "fixed":
            # Ohne Version im Dateinamen gibt es keinen Versionsabgleich. Statt
            # dessen muss das Manifest denselben Slug nennen: das fängt ein
            # fremdes Bundle ab, das unter dem eigenen Namen im Artefakt liegt.
            if str(manifest.get("name")) != slug:
                die(f"{name}: Name im Manifest {manifest.get('name')!r} passt nicht zum "
                    f"Slug {slug!r}")
        elif str(manifest.get("version")) != m.group("version"):
            die(f"{name}: manifest.version {manifest.get('version')!r} passt nicht zur "
                f"Version im Dateinamen {m.group('version')!r}")
        files.append({
            "name": name, "data": data, "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "family": slug if m is None else slug + (m.group("variant") or ""),
            "version": None if m is None else m.group("version"),
        })
    return files


# --- WebDAV -----------------------------------------------------------------

def join_target(base, folder):
    """Basis und Ordner zu einer URL - OHNE Kodierung, damit das Secret wörtlich
    darin steht und GitHubs Maskierung im Log greift."""
    return base.rstrip("/") + "/" + folder.strip("/")


def normalize_dav_url(url):
    """Pfad prozent-kodieren (Leerzeichen, @ ...). '%' bleibt, damit eine schon
    kodierte URL unverändert durchgeht."""
    p = urllib.parse.urlsplit(url.rstrip("/"))
    return urllib.parse.urlunsplit((p.scheme, p.netloc, urllib.parse.quote(p.path, safe="/%"), "", ""))


def parse_multistatus(payload, base_path):
    """207-Body -> direkte Kinder des Zielordners: [{"name", "is_collection"}].

    Hrefs kommen prozent-kodiert und server-absolut (manchmal als volle URL);
    beides wird dekodiert und gegen den dekodierten Basis-Pfad verglichen.
    Wirft ValueError, wenn das XML unlesbar ist oder keine Antwort zum Basis-Pfad
    passt - dann stimmen Basis-URL und Server-Sicht nicht überein, und es darf
    nichts daraus abgeleitet werden.
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
        self.base = normalize_dav_url(target)
        self.base_path = urllib.parse.urlsplit(self.base).path
        token = base64.b64encode(f"{user}:{password}".encode()).decode()
        self.auth = "Basic " + token

    def request(self, method, name="", data=None, headers=None, timeout=120):
        """(status, body). 0 = kein Ergebnis nach 3 Versuchen. Wiederholt Netzfehler,
        Locks (423), Drosselung (429) und 5xx mit Pausen 2 s, 4 s.
        Meldungen enthalten nie die URL - und damit nie den Login."""
        url = self.base + ("/" + urllib.parse.quote(name, safe="") if name else "")
        for attempt in range(3):
            req = urllib.request.Request(url, data=data, method=method)
            req.add_header("Authorization", self.auth)
            req.add_header("User-Agent", "ent-publish-sciebo/2")
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

def write_summary(slug, folder, rows):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    lines = [f"### sciebo: {slug}", "", f"Ordner: `{folder}`", "",
             "| Datei | Aktion | Größe | SHA-256 |", "|---|---|---|---|"]
    for name, action, size, sha in rows:
        size_s = f"{size / 1024:.0f} KB" if size else "-"
        sha_s = f"`{sha[:16]}`" if sha else "-"
        lines.append(f"| `{name}` | {action} | {size_s} | {sha_s} |")
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n\n")


def main(argv):
    if "--self-test" in argv:
        return self_test()
    dry_run = "--dry-run" in argv
    rest = [a for a in argv if a != "--dry-run"]
    if any(a.startswith("-") for a in rest) or len(rest) > 1:
        die("Aufruf: publish_sciebo.py [--dry-run] [DIST_DIR] | --self-test")
    dist_dir = rest[0] if rest else None
    if not dry_run and not dist_dir:
        die("DIST_DIR fehlt - ohne --dry-run ist das Verzeichnis mit den Bundles Pflicht")

    slug, root, folder, user, password = read_env()
    target = join_target(root, folder)
    dav = Dav(target, user, password)
    if IN_CI:
        # Der Login ist ein Secret; seine kodierte Form (%40 statt @) weicht davon ab
        # und würde sonst nicht maskiert. Die ::add-mask::-Zeilen selbst erscheinen nicht im Log.
        log("::add-mask::" + urllib.parse.quote(user, safe=""))
        log("::add-mask::" + dav.auth)
    # Unkodiert ausgeben: der Login steht wörtlich darin, GitHub maskiert ihn.
    log(f"Ziel: {target}")
    owned = f"{slug}-*.{EXT}" if MODE == "versioned" else f"{slug}.{EXT} und {slug}-*.{EXT}"
    log(f"Slug: {slug}  - Modus: {MODE}  - Eigentum: {owned}, sonst nichts"
        + (f"  - fremd: {', '.join(OTHER_SLUGS)}" if OTHER_SLUGS else ""))
    if dry_run:
        log("DRY RUN - es wird nichts hochgeladen und nichts gelöscht")

    local = collect_local(dist_dir, slug) if dist_dir else []
    for f in local:
        log(f"  gebaut: {f['name']}  {f['size'] / 1024:.0f} KB  sha256 {f['sha256'][:16]}")

    # 1. Vorher-Bild - zugleich die Existenzprüfung des Zielordners
    status, before = dav.listing()
    if before is None:
        die(f"Zielordner nicht lesbar: {explain(status) if status != 207 else 'Antwort unlesbar'}")
    log(f"Ordner vorher ({len(before)} Einträge):")
    for e in before:
        kind = "ordner" if e["is_collection"] else ("eigen " if owns_file(slug, e["name"]) else "fremd ")
        log(f"  [{kind}] {e['name']}")
    before_names = {e["name"] for e in before}

    if dry_run:
        planned = {f["name"] for f in local}
        would_delete = [e["name"] for e in before
                        if owns_file(slug, e["name"]) and not e["is_collection"] and e["name"] not in planned] if local else []
        log("Würde hochladen: " + (", ".join(sorted(planned)) or "nichts (kein DIST_DIR)"))
        log("Würde löschen:   " + (", ".join(would_delete) or
                                   ("nichts" if local else "nichts - ohne Upload-Menge wird nie gelöscht")))
        log("DRY RUN beendet - nichts verändert.")
        return 0

    # 2. Upload - vor dem Aufräumen, damit es keinen Moment ohne Bundle gibt
    rows, ok_files, failed = [], [], []
    for f in local:
        status, _ = dav.request("PUT", f["name"], data=f["data"])
        if status in (200, 201, 204):
            log(f"  PUT {f['name']} -> {status} ({'überschrieben' if f['name'] in before_names else 'neu'})")
            ok_files.append(f)
        else:
            log(f"  x PUT {f['name']} -> {explain(status)}")
            failed.append(f)
            rows.append((f["name"], f"Upload fehlgeschlagen ({explain(status)})", f["size"], f["sha256"]))

    # 3. Zurücklesen: erst wenn die Bytes in sciebo denen des Release-Assets gleichen,
    #    gilt der Upload - und erst dann darf der Vorgänger dieser Datei gehen.
    verified = []
    for f in ok_files:
        status, body = dav.request("GET", f["name"])
        if status == 200 and hashlib.sha256(body).hexdigest() == f["sha256"]:
            log(f"  OK  {f['name']} zurückgelesen, byte-identisch")
            verified.append(f)
        else:
            why = explain(status) if status != 200 else "Inhalt weicht ab"
            log(f"  x   {f['name']}: Nachprüfung fehlgeschlagen ({why})")
            failed.append(f)
            rows.append((f["name"], f"hochgeladen, Nachprüfung fehlgeschlagen ({why})", f["size"], f["sha256"]))

    # 4. Nachher-Liste, dann aufräumen - nur Eigenes, nur .mcpb, nur nicht gerade Hochgeladenes
    status, after = dav.listing()
    after_names = {e["name"] for e in after} if after is not None else set()
    deleted, delete_failed, kept, sweep_skipped = [], [], [], False
    if after is None:
        sweep_skipped = True
        warn("Ordnerliste nach dem Upload nicht lesbar - es wird nichts gelöscht. Überholte Dateien "
             "bleiben liegen; den Job erneut ausführen.", "PROPFIND fehlgeschlagen")
    elif not verified:
        sweep_skipped = True
        warn("Kein Bundle erfolgreich hochgeladen und bestätigt - es wird nichts gelöscht.", "Kein Upload")
    else:
        for f in list(verified):
            if f["name"] not in after_names:
                warn(f"{f['name']} fehlt in der Ordnerliste nach dem Upload - der Vorgänger bleibt.", "Nachprüfung")
                verified.remove(f)
                failed.append(f)
                rows.append((f["name"], "hochgeladen, aber nicht in der Liste", f["size"], f["sha256"]))
        complete = not failed
        uploaded = {f["name"] for f in verified}
        families_ok = {f["family"] for f in verified}
        for f in verified:
            rows.append((f["name"], "hochgeladen, byte-identisch bestätigt", f["size"], f["sha256"]))
        for e in after:
            name = e["name"]
            if not owns_file(slug, name) or name in uploaded:
                continue
            if e["is_collection"]:
                warn(f"{name} ist ein Ordner - wird nicht gelöscht.", "Unerwarteter Ordner")
                kept.append(name)
                continue
            if not complete and family_of(slug, name) not in families_ok:
                log(f"  bleibt: {name} (Teilausfall - Nachfolger dieser Familie nicht bestätigt)")
                kept.append(name)
                rows.append((name, "bleibt (Teilausfall)", 0, ""))
                continue
            guard_owned(slug, name)  # HARD GUARD 2 - wirft, statt zu löschen
            status, _ = dav.request("DELETE", name, timeout=60)
            if status in (200, 204, 404):
                log(f"  DELETE {name} -> {status}" + (" (war schon weg)" if status == 404 else ""))
                deleted.append(name)
                rows.append((name, "gelöscht (überholt)", 0, ""))
            else:
                log(f"  x DELETE {name} -> {explain(status)}")
                delete_failed.append(name)
                rows.append((name, f"Löschen fehlgeschlagen ({explain(status)})", 0, ""))

    if after is not None:
        remaining = sorted((after_names - set(deleted)) | {f["name"] for f in verified})
        log(f"Ordner nachher ({len(remaining)} Einträge, berechnet):")
        for name in remaining:
            log(f"  [{'eigen ' if owns_file(slug, name) else 'fremd '}] {name}")
    write_summary(slug, folder, rows)

    log("---")
    problems = []
    if failed:
        problems.append(f"{len(failed)} Upload(s) nicht bestätigt")
    if delete_failed:
        problems.append(f"{len(delete_failed)} Löschung(en) fehlgeschlagen")
    if sweep_skipped and verified:
        problems.append("Aufräumen übersprungen")
    if problems:
        die("FEHLGESCHLAGEN: " + "; ".join(problems) + ". Nichts Fremdes wurde berührt. Job erneut ausführen.")
    log(f"OK: {len(verified)} Bundle(s) hochgeladen und bestätigt, {len(deleted)} überholte(s) entfernt, "
        f"{len(kept)} behalten.")
    return 0


# --- Selbsttest (offline) -------------------------------------------------------

def self_test():
    checks = 0

    def ok(cond, what):
        nonlocal checks
        checks += 1
        if not cond:
            die(f"Selbsttest fehlgeschlagen: {what}")

    known = ("confluence", "github-access", "sciebo-files", "uni-mail")
    for slug, name, expected in [
        ("uni-mail", "uni-mail-exchange-v1.1.1.mcpb", True),
        ("uni-mail", "uni-mail-v0.9.0.mcpb", True),
        ("uni-mail", "uni-mail-latest.mcpb", True),
        ("uni-mail", "uni-mailer-v1.0.0.mcpb", False),
        ("uni-mail", "sciebo-files-v0.1.1.mcpb", False),
        ("uni-mail", "VERSIONS.md", False),
        ("uni-mail", ".version.json", False),
        ("uni-mail", "uni-mail-v1.0.0.mcpb.bak", False),
        ("uni-mail", "uni-mail-x/../confluence-v0.4.0.mcpb", False),
        ("uni-mail", "uni-mail-v1.0.0.mcpb\n", False),
        ("uni-mail", "", False),
        ("sciebo-files", "sciebo-files-v0.1.1.mcpb", True),
        ("sciebo-files", "sciebo-v1.0.0.mcpb", False),
        ("sciebo-files", "sciebo-files.mcpb", False),
        ("confluence", "confluence-v0.4.0.mcpb", True),
        ("github-access", "github-access-v1.2.0.mcpb", True),
        ("github-access", "github-v1.2.0.mcpb", False),
    ]:
        others = tuple(k for k in known if k != slug)
        ok(owns_file(slug, name, others) is expected, f"owns_file({slug!r}, {name!r}) != {expected}")

    # Fremd-Präfix: ein Name, der mit "<anderer-slug>-" beginnt, gehört nie dem eigenen Slug,
    # auch wenn er zugleich mit "<slug>-" beginnt.
    ok(owns_file("uni", "uni-mail-cfm-v1.1.1.mcpb", ("uni-mail",)) is False, "fremd-präfix uni/uni-mail")
    ok(owns_file("uni", "uni-v1.0.0.mcpb", ("uni-mail",)) is True, "eigenes trotz other")
    ok(owns_file("sciebo", "sciebo-files-v0.1.1.mcpb", ("sciebo-files",)) is False, "fremd-präfix sciebo/sciebo-files")
    ok(owns_file("uni-mail", "uni-mail-cfm-v1.1.1.mcpb", ()) is True, "ohne others")

    # Modus fixed: der blosse Name gehört dazu, das Präfix bleibt eigen (Altlasten
    # aus versionierten Läufen), alles andere nicht.
    plugins = ("ent-thesis", "ent-aem", "ent-access")
    for slug, name, expected in [
        ("ent-thesis", "ent-thesis.plugin", True),
        ("ent-thesis", "ent-thesis-v1.2.0.plugin", True),
        ("ent-thesis", "ent-thesis-alt.plugin", True),
        ("ent-thesis", "ent-thesis.mcpb", False),
        ("ent-thesis", "ent-thesisX.plugin", False),
        ("ent-thesis", "ent-aem.plugin", False),
        ("ent-thesis", "VERSIONS.md", False),
        ("ent-thesis", "ent-thesis.plugin.bak", False),
        ("ent-thesis", "ent-thesis-x/../ent-aem.plugin", False),
    ]:
        others = tuple(k for k in plugins if k != slug)
        ok(owns_file(slug, name, others, mode="fixed", ext="plugin") is expected,
           f"owns_file fixed({slug!r}, {name!r}) != {expected}")

    # Kreuzproben: der blosse Name gehört nur im Modus fixed dazu, eine fremde
    # Endung in keinem Modus.
    ok(owns_file("ent-thesis", "ent-thesis.plugin", (), mode="versioned", ext="plugin") is False,
       "blosser Name gehört im Modus versioned nicht dazu")
    ok(owns_file("ent-thesis", "ent-thesis-v1.0.0.plugin", (), mode="versioned", ext="plugin") is True,
       "Präfix gehört im Modus versioned dazu")
    ok(owns_file("ent-thesis", "ent-thesis-v1.0.0.mcpb", (), mode="fixed", ext="plugin") is False,
       "fremde Endung nie (plugin-Lauf)")
    ok(owns_file("uni-mail", "uni-mail-v1.0.0.plugin", (), mode="versioned", ext="mcpb") is False,
       "fremde Endung nie (mcpb-Lauf)")

    # Ungültiger Modus / ungültige Endung wirft wie ein ungültiger Slug.
    for bad_mode in ("", "Versioned", "fix", "latest"):
        try:
            owns_file("ent-thesis", "ent-thesis.plugin", (), mode=bad_mode, ext="plugin")
            ok(False, f"owns_file akzeptiert ungültigen Modus {bad_mode!r}")
        except ValueError:
            ok(True, "")
    for bad_ext in ("", ".plugin", "MCPB", "zip"):
        try:
            owns_file("ent-thesis", "ent-thesis.plugin", (), mode="fixed", ext=bad_ext)
            ok(False, f"owns_file akzeptiert ungültige Endung {bad_ext!r}")
        except ValueError:
            ok(True, "")

    for bad in ("", "Uni-Mail", "uni_mail", "-uni", "uni-", "uni--mail", "uni mail", "uni.mail", "uni-mail\n"):
        ok(not SLUG_RE.fullmatch(bad), f"Slug {bad!r} dürfte nicht gültig sein")
        try:
            owns_file(bad, "x-v1.0.0.mcpb")
            ok(False, f"owns_file akzeptiert ungültigen Slug {bad!r}")
        except ValueError:
            ok(True, "")

    r = bundle_re("uni-mail")
    for name, expected in [
        ("uni-mail-exchange-v1.1.1.mcpb", True),
        ("uni-mail-v0.9.0.mcpb", True),
        ("uni-mail-v1.2.0-rc.1.mcpb", True),
        ("uni-mail-latest.mcpb", False),
        ("uni-mail-exchange-latest.mcpb", False),
        ("uni-mail-v1.0.mcpb", False),
        ("uni-mail-v1.1.1.mcpb.mcpb", False),
        ("uni-mail-v1.0.0+1.mcpb", False),
        ("uni-mail-Exchange-v1.1.1.mcpb", False),
        ("uni-mail-exchange-cfm-v1.1.1.mcpb", False),
        ("uni-mailer-v1.0.0.mcpb", False),
        ("uni-mail-v1.0.0.mcpb\n", False),
    ]:
        ok(bool(r.fullmatch(name)) is expected, f"bundle_re({name!r}) != {expected}")
    ok(family_of("uni-mail", "uni-mail-exchange-v1.1.1.mcpb") == "uni-mail-exchange", "family variante")
    ok(family_of("uni-mail", "uni-mail-v0.9.0.mcpb") == "uni-mail", "family ohne variante")
    ok(family_of("uni-mail", "uni-mail-latest.mcpb") is None, "family ohne schema")
    ok(family_of("sciebo-files", "sciebo-files-v0.1.1.mcpb") == "sciebo-files", "family sciebo")

    # bundle_re trennt die Endungen, und im Modus fixed ist der Namensraum eine Familie.
    ok(bool(bundle_re("ent-thesis", "plugin").fullmatch("ent-thesis-v1.2.0.plugin")),
       "bundle_re plugin trifft .plugin")
    ok(not bundle_re("ent-thesis", "plugin").fullmatch("ent-thesis-v1.2.0.mcpb"),
       "bundle_re plugin trifft keine .mcpb")
    ok(not bundle_re("ent-thesis", "mcpb").fullmatch("ent-thesis-v1.2.0.plugin"),
       "bundle_re mcpb trifft keine .plugin")
    ok(bool(bundle_re("ent-thesis").fullmatch("ent-thesis-v1.2.0.mcpb")), "bundle_re Standard mcpb")
    ok(family_of("ent-thesis", "ent-thesis.plugin", mode="fixed", ext="plugin") == "ent-thesis",
       "family fixed blosser Name")
    ok(family_of("ent-thesis", "ent-thesis-v1.2.0.plugin", mode="fixed", ext="plugin") == "ent-thesis",
       "family fixed Altlast")
    ok(family_of("ent-thesis", "ent-thesis-alt.plugin", mode="fixed", ext="plugin") == "ent-thesis",
       "family fixed ohne Schema")
    ok(family_of("ent-thesis", "ent-aem.plugin", mode="fixed", ext="plugin") is None,
       "family fixed fremd")
    ok(family_of("ent-thesis", "ent-thesis.plugin", mode="versioned", ext="plugin") is None,
       "family versioned kennt den blossen Namen nicht")

    for slug, name in [("uni-mail", "sciebo-files-v0.1.1.mcpb"), ("uni-mail", "VERSIONS.md"),
                       ("uni-mail", "uni-mailer-v1.0.0.mcpb"), ("sciebo-files", "sciebo-v1.0.0.mcpb")]:
        try:
            guard_owned(slug, name)
            ok(False, f"guard_owned({slug!r}, {name!r}) hat nicht ausgelöst")
        except RuntimeError:
            ok(True, "")
    ok(guard_owned("uni-mail", "uni-mail-cfm-v1.1.1.mcpb"), "guard eigene Datei")

    # Passendes Präfix, aber fremde Endung: kein DELETE.
    for slug, name, ext in [("ent-thesis", "ent-thesis-v1.0.0.mcpb", "plugin"),
                            ("ent-thesis", "ent-thesis.plugin", "mcpb"),
                            ("uni-mail", "uni-mail-v1.0.0.plugin", "mcpb")]:
        try:
            guard_owned(slug, name, ext)
            ok(False, f"guard_owned({slug!r}, {name!r}, {ext!r}) hat nicht ausgelöst")
        except RuntimeError:
            ok(True, "")

    # Der Löschpfad in main() ruft guard_owned und family_of ohne Modus- und
    # Endungsargument, also über die Globals - hier werden sie kurz umgestellt.
    global MODE, EXT
    saved_mode, saved_ext = MODE, EXT
    try:
        MODE, EXT = "fixed", "plugin"
        ok(guard_owned("ent-thesis", "ent-thesis.plugin"), "guard fixed: blosser Name")
        ok(guard_owned("ent-thesis", "ent-thesis-v1.2.0.plugin"), "guard fixed: Altlast")
        for name in ("ent-aem.plugin", "ent-thesis.mcpb", "VERSIONS.md"):
            try:
                guard_owned("ent-thesis", name)
                ok(False, f"guard_owned fixed hat {name!r} nicht abgelehnt")
            except RuntimeError:
                ok(True, "")
        ok(family_of("ent-thesis", "ent-thesis.plugin") == "ent-thesis", "family fixed über Globals")
    finally:
        MODE, EXT = saved_mode, saved_ext

    ok(join_target("https://h/remote.php/dav/files/u@x.de/", "/A B/C/") ==
       "https://h/remote.php/dav/files/u@x.de/A B/C", "join_target")
    ok(dav_root("https://h", "u@x.de") == "https://h/remote.php/dav/files/u@x.de", "dav_root kurz")
    ok(dav_root("https://h/", "u@x.de") == "https://h/remote.php/dav/files/u@x.de", "dav_root kurz mit Slash")
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
    ok(normalize_dav_url("https://h/remote.php/dav/files/u@x.de/A B/C/") ==
       "https://h/remote.php/dav/files/u%40x.de/A%20B/C", "normalize_dav_url")
    ok(normalize_dav_url("https://h/remote.php/dav/files/u%40x.de/A%20B/C") ==
       "https://h/remote.php/dav/files/u%40x.de/A%20B/C", "normalize idempotent")

    xml = (b'<?xml version="1.0"?><d:multistatus xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">'
           b'<d:response><d:href>/remote.php/dav/files/u%40x.de/A%20B/C/</d:href>'
           b'<d:propstat><d:prop><d:resourcetype><d:collection/></d:resourcetype></d:prop>'
           b'<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>'
           b'<d:response><d:href>/remote.php/dav/files/u%40x.de/A%20B/C/uni-mail-exchange-v1.1.1.mcpb</d:href>'
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
       [(".version.json", False), ("Sub Folder", True), ("uni-mail-exchange-v1.1.1.mcpb", False)],
       f"parse_multistatus: {entries}")
    for bad_payload, bad_base, what in [(b"<not xml", "/x", "kaputtes XML"),
                                        (xml, "/remote.php/webdav/A%20B/C", "falsche Basis")]:
        try:
            parse_multistatus(bad_payload, bad_base)
            ok(False, f"parse_multistatus hat {what} akzeptiert")
        except ValueError:
            ok(True, "")

    log(f"Selbsttest OK: {checks} Prüfungen bestanden")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
