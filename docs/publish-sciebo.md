# publish-sciebo

Lädt die `.mcpb` aus einem Artefakt desselben Laufs per WebDAV in den sciebo-Ordner des aufrufenden
Repositories, für alle, die nicht über GitHub installieren, und entfernt dort anschließend die
überholten Bundles derselben Extension.

Datei: [`.github/workflows/publish-sciebo.yml`](../.github/workflows/publish-sciebo.yml)

## Voraussetzung ist ein Artefakt, nicht ein bestimmter Workflow

Dieser Workflow setzt [`release`](release.md) nicht voraus. Er braucht nur einen Vorgänger-Job im
selben Lauf, der ein Artefakt mit `.mcpb`-Dateien ablegt, deren Namen mit `<slug>-` beginnen. Der
Artefaktname ist einstellbar; das Artefakt wird über das Runtime-Token des Laufs geladen, nicht
über `GITHUB_TOKEN`.

## Aufruf

```yaml
jobs:
  publish-sciebo:
    needs: build
    uses: MuensterEntrepreneurship/.github/.github/workflows/publish-sciebo.yml@v1
    with:
      slug: sciebo
      version: ${{ needs.build.outputs.version }}
      artifact: mcpb-bundles
    secrets: inherit
    permissions:
      contents: read
```

Zusammen mit `release` übernimmt man Slug und Version besser aus dessen Ausgaben, statt sie erneut
zu schreiben:

```yaml
    with:
      slug: ${{ needs.release.outputs.slug }}
      version: ${{ needs.release.outputs.version }}
```

## Eingaben

| Eingabe | Pflicht | Bedeutung |
|---------|---------|-----------|
| `slug` | ja | Präfix der Dateien, die dieser Lauf hochlädt und aufräumen darf. Muss in der Allowlist stehen. |
| `version` | nein | Plausibilitätsprüfung: jede Datei muss auf `-v<version>.mcpb` enden |
| `artifact` | nein | Name des Artefakts mit den Bundles, Standard `mcpb-bundles` |

## Secrets und Variablen des aufrufenden Repositories

`secrets.*` und `vars.*` lösen sich immer gegen das aufrufende Repository auf, nie gegen dieses
`.github`-Repository. Jedes aufrufende Repo setzt sie also selbst, unter Settings, Secrets and
variables, Actions:

| Name | Art | Wert |
|------|-----|------|
| `SCIEBO_USER` | Secret | sciebo-Login |
| `SCIEBO_APP_PASSWORD` | Secret | App-Passwort aus sciebo, Einstellungen, Sicherheit |
| `SCIEBO_BASE_URL` | Variable | Nur der Host, `https://uni-muenster.sciebo.de`. Das Skript ergänzt `/remote.php/dav/files/<SCIEBO_USER>`. |
| `SCIEBO_FOLDER` | Variable | Zielordner unterhalb der Dateien-Wurzel. Muss existieren, der Workflow legt ihn nicht an. |

Eine nicht gesetzte Variable expandiert zu `""` statt zu scheitern. Der erste Schritt fängt das ab,
statt in die Konto-Wurzel zu veröffentlichen.

## Aufräumregel

Mehrere Repositories teilen sich denselben sciebo-Ordner, jedes räumt nur hinter sich selbst auf.
Die Regel, und es gibt nur diese eine: erst hochladen, dann den Ordner per PROPFIND lesen, und nur
`.mcpb` löschen, die diesem Slug gehören und nicht gerade in diesem Lauf hochgeladen wurden.
Scheitert der PROPFIND, wird nichts gelöscht.

Eigentum heißt: der Dateiname beginnt mit `<slug>-`, endet auf `.mcpb` und beginnt nicht mit
`<anderer-slug>-` eines der übrigen bekannten Slugs. Sonst nichts. Es gibt keine Zustandsdatei; die
Ordnerliste ist der Zustand, die Version steht im Dateinamen.

Geprüft wird das dreifach: vor dem ersten Byte gegen die Dateinamen im Artefakt, dann in
`owns_file` im Skript, und ein drittes Mal in `guard_owned` unmittelbar vor jedem `DELETE`.

## Kurznamen (Slugs)

| Slug | Repository |
|------|------------|
| confluence | mcp-confluence |
| github-access | mcp-github-access |
| sciebo | mcp-sciebo |
| uni-mail | mcp-uni-mail |

Die Allowlist lebt in `KNOWN_SLUGS` in der Workflow-Datei. Kein Slug darf ein anderer Slug plus
`-…` sein, in keiner der beiden Richtungen: das Slug-Präfix ist die einzige Grundlage dafür, welche
Dateien ein Lauf löschen darf. Der Workflow prüft die Regel paarweise über die gesamte Allowlist
und lehnt sonst ab.

Eine neue Extension aufnehmen:

1. Slug in `KNOWN_SLUGS` in `.github/workflows/publish-sciebo.yml` eintragen
2. Zeile in der Tabelle oben ergänzen
3. Prüfen, dass der neue Slug kein Präfix eines bestehenden ist und umgekehrt
4. Secrets und Variablen im neuen Repository setzen
5. `v1` erst nachziehen, wenn beides zusammen auf `main` liegt

## Skript und Versionierung

Die Logik steht in [`scripts/publish_sciebo.py`](../scripts/publish_sciebo.py), nicht im Workflow.
Ein wiederverwendbarer Workflow kann sein eigenes Repository nicht per `actions/checkout`
auschecken, Checkout und `github.*` gehören dem Aufrufer. Über den `job`-Kontext kennt er aber seine
eigene Herkunft: `job.workflow_repository` und `job.workflow_sha` zeigen auf die Datei, die den Job
definiert. Der Workflow lädt das Skript genau an dieser SHA nach `$RUNNER_TEMP` und schreibt die
Herkunft in die Zusammenfassung des Laufs. Workflow-Datei und Skript stammen damit immer aus
demselben Commit.

Das Skript trägt einen Selbsttest der Schutzlogik (`--self-test`). Er läuft in der CI dieses
Repositories bei jedem Push und Pull Request und zusätzlich in jedem Lauf, bevor die Zugangsdaten
überhaupt in die Umgebung gelangen.

Aufrufer referenzieren `@v1`, nicht `@main`. `v1` ist ein bewusst verschobener Tag: eine Änderung
auf `main` wird erst wirksam, wenn `v1` nachgezogen wird. Zusätzlich trägt jede Änderung einen
unveränderlichen Tag `v1.x.y`. Der Grund: ein Lauf läuft mit den Secrets des aufrufenden
Repositories, hier also mit einem fremden sciebo-App-Passwort.
