#!/usr/bin/env python3
"""publish_sciebo.py - EINE Extension nach sciebo, sonst nichts.

Lädt die gerade gebauten .mcpb einer Extension in den sciebo-Ordner und entfernt
danach die überholten Bundles DERSELBEN Extension. Alles andere im Ordner gehört
anderen Repos und wird nie angefasst. Es gibt keine Zustandsdatei mehr: die
Ordnerliste ist der Zustand, die Version steht im Dateinamen.

Eigentum = Dateiname beginnt mit "<slug>-" und endet auf ".mcpb" - und beginnt
NICHT mit "<anderer-slug>-" für einen der übrigen bekannten Slugs. Nur das.

Aufruf:
  publish_sciebo.py DIST_DIR              DIST_DIR/*.mcpb hochladen, dann aufräumen
  publish_sciebo.py --dry-run [DIST_DIR]  nur PROPFIND; zeigt, was passieren würde
  publish_sciebo.py --self-test           Offline-Prüfung der Schutzlogik, kein Netz

Umgebung:
  SLUG                 Kurzname der Extension, z.B. sciebo oder uni-mail
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
# <slug>[-<variante>]-v<major>.<minor>.<patch>[-<prerelease>].mcpb
# Prerelease nur mit Bindestrich (SemVer), damit "x-v1.0.0.mcpb.mcpb" nicht passt.
BUNDLE_RE_TEMPLATE = (r"{slug}(?P<variant>-[a-z0-9]+)?-v(?P<version>[0-9]+\.[0-9]+\.[0-9]+"
                      r"(-[0-9A-Za-z][0-9A-Za-z.-]*)?)\.mcpb")
OTHER_SLUGS = ()  # wird in read_env() gesetzt; owns_file() nimmt alternativ `others`


def log(msg):
    print(msg, flush=True)


def warn(msg, title="publish-sciebo"):
    log(f"::warning title={title}::{msg}" if IN_CI else f"WARNUNG: {msg}")


def die(msg, code=1):
    log(f"::error::{msg}" if IN_CI else f"FEHLER: {msg}")
    sys.exit(code)


# --- Eigentum: die eine Funktion, die entscheidet ---------------------------

def owns_file(slug, name, others=None):
    """True genau dann, wenn NAME ein Bundle dieser Extension ist.

    Präfix "<slug>-" und Suffix ".mcpb", sonst nichts. Absichtlich kein Regex auf
    die Version: auch ein von Hand abgelegtes "uni-mail-test.mcpb" gehört uni-mail
    und darf von uni-mail weggeräumt werden - "uni-mailer-v1.0.0.mcpb" aber nie,
    und "VERSIONS.md" oder ".version.json" erst recht nicht.

    Beginnt der Name mit "<other>-" für einen anderen bekannten Slug, gehört er
    nie dieser Extension - auch dann nicht, wenn er zugleich mit "<slug>-" beginnt.
    """
    if not isinstance(slug, str) or not SLUG_RE.fullmatch(slug):
        raise ValueError(f"ungültiger Slug: {slug!r}")
    others = OTHER_SLUGS if others is None else tuple(others)
    if not isinstance(name, str) or not name:
        return False
    if "/" in name or "\\" in name or name in (".", ".."):
        return False
    if not (name.startswith(slug + "-") and name.endswith(".mcpb")):
        return False
    for other in others:
        if other != slug and name.startswith(other + "-"):
            return False
    return True


def guard_owned(slug, name):
    """Re-Assertion unmittelbar vor jedem DELETE: wirft, statt zu löschen.

    Absichtlich kein `assert` - das fällt unter `python3 -O` weg.
    """
    if not owns_file(slug, name):
        raise RuntimeError(f"Schutz ausgelöst: {name!r} gehört nicht zu {slug!r} - kein DELETE")
    if not name.endswith(".mcpb"):
        raise RuntimeError(f"Schutz ausgelöst: {name!r} ist keine .mcpb - kein DELETE")
    return True


def bundle_re(slug):
    return re.compile(BUNDLE_RE_TEMPLATE.format(slug=re.escape(slug)))


def family_of(slug, name):
    """'uni-mail-exchange-v1.1.1.mcpb' -> 'uni-mail-exchange'; ohne Schema -> None."""
    m = bundle_re(slug).fullmatch(name)
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
    global OTHER_SLUGS
    # Leer zählt als fehlend: eine nicht gesetzte GitHub-Variable expandiert zu "",
    # und ein leerer Ordner hieße "in die Kontowurzel veröffentlichen".
    missing = [v for v in ENV_VARS if not os.environ.get(v, "").strip()]
    if missing:
        die("Nicht gesetzt (leer zählt als fehlend): " + ", ".join(missing))
    slug = os.environ["SLUG"].strip()
    if not SLUG_RE.fullmatch(slug):
        die(f"SLUG {slug!r} ungültig - erlaubt: Kleinbuchstaben, Ziffern, einzelne Bindestriche")
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


def collect_local(dist_dir, slug):
    """Alle DIST_DIR/*.mcpb - jede muss zum Slug und zum Namensschema passen,
    sonst Abbruch, bevor sciebo berührt wird (HARD GUARD 1)."""
    if not os.path.isdir(dist_dir):
        die(f"Verzeichnis nicht gefunden: {dist_dir}")
    names = sorted(n for n in os.listdir(dist_dir) if n.endswith(".mcpb"))
    if not names:
        die(f"Keine .mcpb in {dist_dir} - nichts zu veröffentlichen")
    pattern = bundle_re(slug)
    files = []
    for name in names:
        path = os.path.join(dist_dir, name)
        if not os.path.isfile(path):
            die(f"{name} ist keine reguläre Datei")
        m = pattern.fullmatch(name)
        if not owns_file(slug, name) or not m:
            die(f"{name}: passt nicht zum Schema {slug}[-variante]-v<major>.<minor>.<patch>.mcpb "
                f"für Slug {slug!r} - Abbruch, bevor sciebo berührt wird")
        with open(path, "rb") as fh:
            data = fh.read()
        if not data:
            die(f"{name} ist leer")
        try:
            with zipfile.ZipFile(path) as z:
                manifest = json.loads(z.read("manifest.json"))
        except (zipfile.BadZipFile, KeyError, ValueError) as exc:
            die(f"{name}: kein lesbares Bundle ({exc})")
        if str(manifest.get("version")) != m.group("version"):
            die(f"{name}: manifest.version {manifest.get('version')!r} passt nicht zur "
                f"Version im Dateinamen {m.group('version')!r}")
        files.append({
            "name": name, "data": data, "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "family": slug + (m.group("variant") or ""),
            "version": m.group("version"),
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
        die("DIST_DIR fehlt - ohne --dry-run ist das Verzeichnis mit den .mcpb Pflicht")

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
    log(f"Slug: {slug}  - Eigentum: {slug}-*.mcpb, sonst nichts"
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

    for slug, name in [("uni-mail", "sciebo-files-v0.1.1.mcpb"), ("uni-mail", "VERSIONS.md"),
                       ("uni-mail", "uni-mailer-v1.0.0.mcpb"), ("sciebo-files", "sciebo-v1.0.0.mcpb")]:
        try:
            guard_owned(slug, name)
            ok(False, f"guard_owned({slug!r}, {name!r}) hat nicht ausgelöst")
        except RuntimeError:
            ok(True, "")
    ok(guard_owned("uni-mail", "uni-mail-cfm-v1.1.1.mcpb"), "guard eigene Datei")

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
