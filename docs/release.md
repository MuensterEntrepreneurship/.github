# release

Baut die Claude-Desktop-Extension (`.mcpb`) des aufrufenden Repositories aus einem Tag
`v<version>`, prüft Manifest, Tools-Liste und Installierbarkeit, erstellt das Release
`v<version>` sowie das Rolling-Release `latest` und legt die Bundles als Artefakt
`mcpb-bundles` ab.

Datei: [`.github/workflows/release.yml`](../.github/workflows/release.yml)

## Aufruf

```yaml
name: Release
on:
  push:
    tags: ["v*"]
concurrency:
  group: release
  cancel-in-progress: false
jobs:
  release:
    uses: MuensterEntrepreneurship/.github/.github/workflows/release.yml@v1
    with:
      slug: sciebo
    secrets: inherit
    permissions:
      contents: write
```

`permissions: contents: write` muss der Aufrufer setzen: Release, Assets und der Tag-Push hängen
daran, und ein aufgerufener Workflow kann Rechte nur einschränken, nie erweitern. Neue Repos geben
`GITHUB_TOKEN` standardmäßig nur Leserechte.

## Eingabe

| Eingabe | Pflicht | Bedeutung |
|---------|---------|-----------|
| `slug` | ja | Kurzname der Extension und Präfix aller Bundle-Dateinamen, `^[a-z0-9]+(-[a-z0-9]+)*$`. Wird nie aus dem Repository-Namen abgeleitet: das Repo heißt `mcp-sciebo`, die Dateien heißen `sciebo-v<version>.mcpb`. |

## Ausgaben

| Ausgabe | Bedeutung |
|---------|-----------|
| `slug` | Der validierte Slug. Ein Folge-Job übernimmt diesen Wert, statt das Literal erneut zu schreiben. |
| `version` | Version ohne `v`, aus dem Tag |
| `files` | Gebaute Bundle-Dateinamen, durch Leerzeichen getrennt |
| `artifact` | Name des Artefakts mit den Bundles (`mcpb-bundles`) |

## Was das aufrufende Repository braucht

- `manifest.json`, `pyproject.toml` und `uv.lock` im Repository-Root. Der Root ist das
  Bundle-Verzeichnis.
- `.mcpbignore` mit mindestens `.github/`, `dist/` und `.claude/`. `mcpb` schließt diese Pfade
  nicht selbst aus; der Workflow prüft nach dem Packen nach und bricht ab, wenn sie im Bundle
  liegen.
- Dieselbe Version in `manifest.json`, `pyproject.toml`, gegebenenfalls `__init__.py` und im Tag
  `v<version>`. Erlaubt ist `v<major>.<minor>.<patch>[-prerelease]`.
- Jedes Tool aus `manifest.json` muss der `ENABLED_TOOLS`-Liste des Servers entsprechen; der
  Workflow vergleicht beide Mengen.
- Optional `variants.json`, siehe unten.

## Varianten

Eine Extension hält genau eine `user_config`, eine Installation erreicht also genau ein Konto. Wo
mehrere getrennte Installationen gewollt sind, beschreibt `variants.json` die Unterschiede, und der
Workflow baut ein Bundle je Eintrag:

- Dateiname `<slug>-<key>-v<version>.mcpb` statt `<slug>-v<version>.mcpb`
- `key` folgt derselben Regel wie der Slug
- Angepasst werden nur `name`, `display_name`, der erste Satz der beiden Beschreibungen, die
  Vorbelegung des EWS-Endpunkts und ein Postfach-Hinweis an jeder Tool-Beschreibung. Der Hinweis ist
  nötig, weil alle Varianten dieselben Tool-Namen exportieren.

Die Logik dafür steht in [`scripts/apply_variant.mjs`](../scripts/apply_variant.mjs).

## Prüfungen vor dem Release

Kein Release und kein Artefakt entstehen, bevor diese Schritte durch sind:

1. `mcpb validate` auf dem Manifest, je Variante
2. Bundle-Inhalt gegen `.github/`, `dist/`, `.claude/`
3. `uv lock --check` im entpackten Bundle, damit `uv` beim Nutzer nicht still neu auflöst
4. Server-Start im entpackten Bundle: ein Bundle kann fehlerfrei validieren und sich trotzdem nicht
   installieren lassen, weil der Paketbau im entpackten Zustand scheitert. Fehlende Zugangsdaten
   dürfen den Start scheitern lassen, der Bau nicht.

## Rolling-Release `latest`

Zusätzlich zu `v<version>` pflegt der Workflow ein Release `latest` mit stabilen Download-URLs
(`<slug>-latest.mcpb`). Assets, die der aktuelle Lauf nicht gebaut hat, werden dort entfernt, bevor
hochgeladen wird; sonst bleiben Dateien umbenannter Varianten als tote, aktuell aussehende
Downloads hängen.

## Skript und Versionierung

`scripts/apply_variant.mjs` liegt in diesem Repository, nicht im Workflow. Ein wiederverwendbarer
Workflow kann sein eigenes Repository nicht per `actions/checkout` auschecken, Checkout und
`github.*` gehören dem Aufrufer. Über den `job`-Kontext kennt er aber seine eigene Herkunft:
`job.workflow_repository` und `job.workflow_sha` zeigen auf die Datei, die den Job definiert. Der
Workflow lädt das Skript genau an dieser SHA nach `$RUNNER_TEMP`. Workflow-Datei und Skript stammen
damit immer aus demselben Commit.

Aufrufer referenzieren `@v1`, nicht `@main`. `v1` ist ein bewusst verschobener Tag: eine Änderung
auf `main` wird erst wirksam, wenn `v1` nachgezogen wird. Zusätzlich trägt jede Änderung einen
unveränderlichen Tag `v1.x.y`. Der Grund: ein Lauf läuft mit den Secrets des aufrufenden
Repositories.

## Weitergabe der Bundles

Das Artefakt `mcpb-bundles` steht im selben Lauf für einen Folge-Job bereit, etwa
[`publish-sciebo`](publish-sciebo.md). Das ist optional; dieser Workflow ist allein
funktionsfähig. Die Aufbewahrungsfrist des Artefakts beträgt 7 Tage, genug für einen erneuten Lauf
des Folge-Jobs.

## Herkunft

Dieser Workflow ersetzt die Monorepo-Automatisierung von
`MuensterEntrepreneurship/mcp-extensions` (archiviert), die dieselbe Logik pro Extension enthielt.
