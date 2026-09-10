# .github

Organisationsprofil und wiederverwendbare Bausteine der Organisation.

## Inhalt

| Pfad | Zweck |
|------|-------|
| `profile/README.md` | Öffentliches Organisationsprofil, gerendert auf `github.com/MuensterEntrepreneurship` |
| `.github/workflows/` | Wiederverwendbare Workflows, jeder einzeln aufrufbar. Reusable Workflows müssen direkt hier liegen; Unterordner löst GitHub nicht auf. Alle haben nur `on: workflow_call` und laufen in diesem Repo nie von selbst. |
| `scripts/` | Hilfsskripte, die die Workflows zur Laufzeit nachladen |
| `docs/` | Eine Anleitung je Workflow: [`release`](docs/release.md), [`publish-sciebo`](docs/publish-sciebo.md) |
| `.github/workflows/ci.yml` | CI dieses Repos: Syntaxprüfung und Selbsttest der Skripte |

## Schreibrecht

Schreibzugriff bleibt bei den Organisationsinhabern; Teams werden hier nicht hinzugefügt. Ein
aufgerufener Workflow läuft mit den Secrets des aufrufenden Repos.

## Organisationsprofil

`profile/README.md` ist das öffentliche Profil. Eine nur für Mitglieder sichtbare Variante lädt
GitHub ausschließlich aus einem separaten, privaten Repository `.github-private`, ebenfalls unter
`profile/README.md`; sie existiert derzeit nicht.
