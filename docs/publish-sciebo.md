# publish-sciebo

Spiegelt die Dateien eines Artefakts desselben Laufs per WebDAV in einen sciebo-Ordner, für alle,
die nicht über GitHub installieren.

Datei: [`.github/workflows/publish-sciebo.yml`](../.github/workflows/publish-sciebo.yml)

## Der Vertrag

> Jede Datei aus dem Artefakt landet unter ihrem eigenen Namen im Zielordner und ersetzt dort die
> gleichnamige Datei. Es wird nie etwas gelöscht, nie ein Ordner angelegt und nie ein Dateiname
> ausgewertet.

Mehr steht nicht darin, und das ist der Punkt. Weil nichts gelöscht wird, gibt es keine Frage, wem
eine Datei gehört, und damit auch keinen Slug, keine Allowlist, keine Präfixregel, keine
Endungsliste und keine Versionsgrammatik. Welche Dateien in sciebo entstehen, entscheidet allein
der Aufrufer über die Namen im Artefakt. Eine `.mcpb`, ein `.plugin`, ein Skill-Zip und eine PDF
sind für diesen Job dasselbe.

## Voraussetzung ist ein Artefakt, nicht ein bestimmter Workflow

Dieser Workflow setzt [`release`](release.md) nicht voraus. Er braucht nur einen Vorgänger-Job im
selben Lauf, der ein Artefakt mit Dateien ablegt. Das Artefakt wird über das Runtime-Token des
Laufs geladen, nicht über `GITHUB_TOKEN`; ein Checkout findet nicht statt.

## Aufruf

Zusammen mit `release`, für die `.mcpb`-Extensions. `release` legt das Artefakt `mcpb-latest` ab,
das die Bundles unter ihren stabilen Namen trägt (`<slug>-latest.mcpb`, bei Varianten
`<slug>-<key>-latest.mcpb`). Das ist zugleich der Standardwert von `artifact`:

```yaml
jobs:
  publish-sciebo:
    needs: release
    uses: MuensterEntrepreneurship/.github/.github/workflows/publish-sciebo.yml@main
    secrets: inherit
    permissions:
      contents: read
```

Für alles andere, hier ein Repo, das seine Bundles selbst baut:

```yaml
jobs:
  publish-sciebo:
    needs: build
    uses: MuensterEntrepreneurship/.github/.github/workflows/publish-sciebo.yml@main
    with:
      artifact: plugin-bundles
    secrets: inherit
    permissions:
      contents: read
```

Ein Artefakt je Lauf, mit allen Dateien darin. Eine Matrix mit einem Job je Datei ist nicht nötig
und nicht erwünscht: sie lädt dasselbe Skript mehrfach nach und schreibt denselben Ordner mehrfach.

## Eingaben

| Eingabe | Pflicht | Bedeutung |
|---------|---------|-----------|
| `artifact` | nein | Name des Artefakts, dessen Dateien gespiegelt werden. Standard `mcpb-latest`. |

Das ist die vollständige Liste. `slug`, `version`, `mode` und `ext` gibt es nicht mehr; sie waren
Werkzeuge der Löschentscheidung, die es nicht mehr gibt.

## Secrets und Variablen des aufrufenden Repositories

`secrets.*` und `vars.*` lösen sich immer gegen das aufrufende Repository auf, nie gegen dieses
`.github`-Repository. Jedes aufrufende Repo setzt sie also selbst, unter Settings, Secrets and
variables, Actions:

| Name | Art | Wert |
|------|-----|------|
| `SCIEBO_USER` | Secret | sciebo-Login |
| `SCIEBO_APP_PASSWORD` | Secret | App-Passwort aus sciebo, Einstellungen, Sicherheit |
| `SCIEBO_BASE_URL` | Variable | Nur der Host, `https://uni-muenster.sciebo.de`. Das Skript ergänzt `/remote.php/dav/files/<SCIEBO_USER>`. |
| `SCIEBO_FOLDER` | Variable | Zielordner unterhalb der Dateien-Wurzel. Muss existieren, der Workflow legt ihn nicht an. Ohne `%` im Namen. |

Eine nicht gesetzte Variable expandiert zu `""` statt zu scheitern. Der erste Schritt fängt das ab,
statt in die Konto-Wurzel zu veröffentlichen.

**In `SCIEBO_FOLDER` darf niemand sonst Dateien ablegen.** Gelöscht wird zwar nie, aber ein
gleichnamiger Upload überschreibt. Ein reiner Verteilordner erfüllt das, ein gemischter
Arbeitsordner nicht. Mehrere Repos dürfen sich einen Ordner teilen, solange sie verschiedene
Dateinamen benutzen.

## Dateinamen

Der Aufrufer bestimmt sie. Das Skript prüft nur, dass ein Name als Dateiname taugt: ein einzelnes
Pfadsegment, höchstens 200 Byte, ohne Steuerzeichen, ohne führenden oder folgenden Leerraum, in
NFC normalisiert, und nicht im Namensraum, den das Skript für sich reserviert (führender Punkt).
Über alles Weitere, Endung inklusive, entscheidet der Aufrufer.

Für die `.mcpb`-Extensions heißt das: in sciebo liegt `sciebo-latest.mcpb`, genau wie am
Rolling-Release auf GitHub. Ein Bundle, ein Name, überall.

## Was nicht passiert

- **Kein DELETE.** Das Skript kennt vier Methoden, `PUT`, `GET`, `PROPFIND` und `MOVE`. Jede andere
  wird abgewiesen, bevor ein Socket aufgeht, und der Selbsttest beweist diese Reihenfolge offline
  mit einem `urlopen`, das jeden Aufruf als Fehler meldet.
- **Kein MKCOL.** Der Zielordner muss existieren. Ein falsch gesetzter `SCIEBO_FOLDER` bricht mit
  404 ab, statt irgendwo einen Ordner anzulegen.
- **Keine Versionskontrolle in sciebo.** Es gibt kein `.version.json`, keine `VERSIONS.md`, keine
  Historie und keinen Versionsvergleich. Welche Version online ist, steht im Bundle und auf GitHub.

Der Preis dafür ist eine Karteileiche: Wird eine Datei zurückgezogen oder umbenannt, bleibt die
alte liegen, und **der Lauf kann das nicht erkennen**. Mehrere Repos teilen sich einen Ordner; die
aktuelle Datei eines anderen Repos sieht von hier aus genauso aus wie eine eigene Karteileiche.
Genau diese Unterscheidung trifft dieser Spiegel nicht mehr, also darf er sie auch nicht andeuten:
er listet den Ordnerinhalt auf, markiert darin nur die Dateien dieses Laufs und urteilt über keine
andere. Aufräumen ist eine menschliche Entscheidung, keine Empfehlung aus einem Log.

## Ersetzen in zwei Schritten

Ein Überschreiben per `PUT` ist nicht atomar. Bricht es ab, stünde eine halbe Datei unter dem
Namen, den alle kennen, und einen Vorgänger zum Zurückfallen gäbe es nicht. Deshalb:

1. `PUT` auf `.upload.<name>`, im Web ausgeblendet, weil der Name mit einem Punkt beginnt
2. `GET` derselben Datei und Vergleich der SHA-256-Summe gegen die gebauten Bytes
3. `MOVE` mit `Overwrite: T` auf `<name>`

Der Zielname wechselt damit von einer vollständigen Datei zur nächsten. Scheitert einer der
Schritte, bleibt die bisherige Datei unberührt, der Lauf schlägt fehl, und die Zwischendatei wird
beim nächsten Lauf überschrieben. Sie heißt deterministisch nach ihrem Ziel, sammelt sich also
nicht an.

Der Zwischenname trägt ein Präfix und keine eigene Endung, und das ist kein Geschmacksurteil:
Nextcloud lehnt einen `PUT` auf einen Namen mit der Endung `.part` mit HTTP 400 ab, weil diese
Endung für seinen eigenen Teil-Upload reserviert ist, und welche Endungen eine Instanz sonst noch
sperrt (`forbidden_filename_extensions`) ist Konfigurationssache. Mit dem Präfix endet die
Zwischendatei auf dieselbe Endung wie ihr Ziel: was als Ziel erlaubt ist, ist damit auch als
Zwischenstand erlaubt.

## Was der Lauf nicht erkennen kann

Ohne Version im Namen und ohne Zustandsdatei lässt sich nicht feststellen, ob sich etwas geändert
hat. Jeder Lauf lädt deshalb alles hoch. Folgen: die Dateiversionierung im sciebo-Konto wächst
entsprechend mit, und jeder Klient mit synchronisiertem Ordner lädt alles neu. Bei kleinen Bundles
ist das nicht der Rede wert, bei großen schon.

## Skript und Versionierung

Die Logik steht in [`scripts/publish_sciebo.py`](../scripts/publish_sciebo.py), nicht im Workflow.
Ein wiederverwendbarer Workflow kann sein eigenes Repository nicht per `actions/checkout`
auschecken, Checkout und `github.*` gehören dem Aufrufer. Über den `job`-Kontext kennt er aber seine
eigene Herkunft: `job.workflow_repository` und `job.workflow_sha` zeigen auf die Datei, die den Job
definiert. Der Workflow lädt das Skript genau an dieser SHA nach `$RUNNER_TEMP` und schreibt die
Herkunft in die Zusammenfassung des Laufs. Workflow-Datei und Skript stammen damit immer aus
demselben Commit.

Das Skript trägt einen Selbsttest (`--self-test`) und einen `--dry-run`, der nur liest. Der
Selbsttest läuft in der CI dieses Repositories bei jedem Push und Pull Request und zusätzlich in
jedem Lauf, bevor die Zugangsdaten überhaupt in die Umgebung gelangen.

Die Aufrufer referenzieren derzeit `@main`. Empfehlenswert ist ein verschobener Tag, weil ein Lauf
mit den Secrets des aufrufenden Repositories läuft, hier also mit einem fremden
sciebo-App-Passwort. Der Wechsel ist ein eigener Schritt: der Tag muss existieren, bevor ein
Aufrufer ihn referenzieren kann, und der Vertrag dieses Workflows ist gegenüber `v1` ein anderer,
also wäre es `v2` und nicht ein verschobenes `v1`.

## Einmalige Handarbeit beim Umstieg

Der Umstieg auf stabile Namen lässt die bisherigen Dateien liegen, denn gelöscht wird nichts. Nach
dem ersten erfolgreichen Lauf je Repo gehören von Hand entfernt:

- die versionierten Bundles im Extensions-Ordner (`sciebo-v0.1.3.mcpb`, `uni-mail-exchange-v1.1.4.mcpb`, …)
- `.version.json` und `VERSIONS.md` in den Marketplace-Ordnern

Der Lauf listet den Ordnerinhalt am Ende auf, die Namen stehen also im Log.
