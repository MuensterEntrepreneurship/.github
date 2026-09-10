# .github

Dieses Repository enthält das Organisationsprofil (`profile/`) und zwei wiederverwendbare GitHub-Actions-Workflows, die die Extension-Repositories des Instituts nutzen, um Claude-Desktop-Extensions (`.mcpb`) zu bauen und zu verteilen.

## Reusable Workflows

| Workflow | Zweck |
|----------|--------|
| `.github/workflows/release.yml` | Baut das Bundle aus einem Tag `v<version>`, prüft Manifest, Tools-Liste und Installation; erstellt Release `v<version>` und Rolling-Release `latest`; übergibt die Bundles als Artefakt `mcpb-bundles`. |
| `.github/workflows/publish-sciebo.yml` | Spiegelt diese Bundles per WebDAV in den sciebo-Ordner des aufrufenden Repos und räumt dort nur ältere Bundles desselben Kurznamens weg. |

## Aufruf

Jedes Extension-Repository hat diese Datei als `.github/workflows/release.yml`, wobei nur `slug:` unterschiedlich ist:

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
    uses: MuensterEntrepreneurship/.github/.github/workflows/release.yml@main
    with:
      slug: sciebo-files
    secrets: inherit
    permissions:
      contents: write
  publish-sciebo:
    needs: release
    uses: MuensterEntrepreneurship/.github/.github/workflows/publish-sciebo.yml@main
    with:
      slug: ${{ needs.release.outputs.slug }}
      version: ${{ needs.release.outputs.version }}
    secrets: inherit
    permissions:
      contents: read
```

## Was ein aufrufendes Repo braucht

- `manifest.json`, `pyproject.toml`, `uv.lock` im Repository-Root (der Root ist das Bundle-Verzeichnis)
- Optional: `variants.json` für mehrere Bundles aus einer Quelle
- `.mcpbignore` mit mindestens `.github/`, `dist/`, `.claude/`
- Version identisch in `manifest.json`, `pyproject.toml` (und `__init__.py` falls vorhanden) und Tag `v<version>`
- Repository-Secrets: `SCIEBO_WEBDAV_BASE` (nur der Host, z. B. `https://uni-muenster.sciebo.de`; das Skript ergänzt `/remote.php/dav/files/<SCIEBO_USER>`, die volle Dateien-Wurzel wird ebenfalls angenommen), `SCIEBO_USER` (der sciebo-Login), `SCIEBO_APP_PASSWORD` (App-Passwort aus sciebo, Einstellungen, Sicherheit)
- Repository-Variable: `SCIEBO_FOLDER`
- GitHub Actions müssen wiederverwendbare Workflows aus öffentlichen Repos der Organisation aufrufen dürfen

## Kurznamen (Slugs)

| Slug | Repository |
|------|------------|
| confluence | mcp-confluence |
| github-access | mcp-github-access |
| sciebo-files | mcp-sciebo |
| uni-mail | mcp-uni-mail |

Der Slug ist das Präfix aller Bundle-Dateinamen (`<slug>-v<version>.mcpb`, mit Varianten `<slug>-<key>-v<version>.mcpb`) und das einzige Kriterium dafür, welche Dateien ein Repo im gemeinsamen sciebo-Ordner löschen darf. Slugs sind erforderliche Eingaben und werden nie vom Repository-Namen abgeleitet. Die Allowlist lebt in `publish-sciebo.yml` (`KNOWN_SLUGS`); eine neue Extension erfordert das Hinzufügen des Slugs dort. Kein Slug darf ein anderer Slug plus `-…` sein (in beide Richtungen); der Workflow prüft dies paarweise und lehnt ab.

## Schreibrecht

Schreibzugriff auf dieses Repository bleibt bei den Organisationsinhabern; Teams werden hier nicht hinzugefügt. Jede Änderung auf `main` läuft beim nächsten Tag jedes Extension-Repositories mit dessen Secrets.

## Herkunft

Diese Workflows ersetzen die Monorepo-Automatisierung von `MuensterEntrepreneurship/mcp-extensions` (archiviert), die dieselbe Logik pro Extension enthielt.
