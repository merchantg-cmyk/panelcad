# Security policy

This is a draft. It applies once the project has a public repository.

## Reporting a vulnerability

Email **contact@improvemaintenance.com**. Do not open a public issue for a
vulnerability, and do not include a customer drawing, a panel photo, an API
key, or a webhook URL.

## What a public build contains

`python scripts/build-release.py --public` does not embed a bug-report
webhook, a Slack bot token, or a shop update feed. Those values are read only
for a shop build, from the environment or from a gitignored
`release.local.env`. Do not commit them. A public build can check the public
GitHub Releases for an update. That repository is https://github.com/merchantg-cmyk/panelcad.

Bug-report logs are redacted before they are saved or posted. Treat a report
zip as private anyway: it can contain a drawing.

## Supported versions

Only the current release line is in scope until the maintainer publishes a
support window.

## History

Do not publish this repository's existing git history. Customer project data
is in older commits. A public remote should be a new repository whose first
commit is a cleaned snapshot (`python scripts/public-snapshot.py`).
