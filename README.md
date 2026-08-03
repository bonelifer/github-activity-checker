# github-org-checker

A command-line tool that checks a GitHub organization for recent activity —
commits, pull requests, new repositories — and can also track issues,
releases, PR quality metrics, stale PRs, and first-time contributors.
Reports can be printed to the console, exported to Markdown, or emailed via
`msmtp`.

## Features

- Daily, weekly, monthly, or custom (`N`-day) reporting windows
- Saved organizations with a default, so you don't have to pass the org name
  every run
- Optional extras (each opt-in, since each adds extra GitHub API calls):
  - `-i, --include-issues` — issues opened/closed/still-open in the period
  - `-I, --include-releases` — new releases/tags published in the period
  - `-p, --pr-metrics` — time-to-merge, time-to-first-review, lines changed
  - `-D, --stale-days DAYS` — flag open PRs idle for `DAYS`+ days
  - `-N, --track-new-contributors` — flag first-time contributors using a
    locally persisted history file
- Markdown export (`-x, --export-md`)
- Email delivery via `msmtp`, with a saved BCC list that's only used when
  explicitly requested on a given send (`-b, --bcc`)

## Requirements

- Python 3.9+
- [`msmtp`](https://marlam.de/msmtp/) if you want email delivery
- A GitHub personal access token is optional but strongly recommended —
  unauthenticated requests are capped at 60/hour

## Installation

```bash
git clone https://github.com/bonelifer/github-org-checker.git
cd github-org-checker
pip install -r requirements.txt
```

Set your token as an environment variable (recommended over `-t/--token` on
the command line, which is visible in shell history and process listings):

```bash
export GITHUB_TOKEN=your_token_here
```

## Usage

```bash
# Weekly report for an organization
python github_org_checker.py my-org --weekly

# Save an organization as the default so you don't need to pass it again
python github_org_checker.py -a my-org -s my-org
python github_org_checker.py --weekly

# Weekly report with issues, releases, and stale-PR detection
python github_org_checker.py my-org -w -i -I -D 14

# Export to Markdown
python github_org_checker.py my-org --monthly -x

# Configure and send email
python github_org_checker.py -c reports@example.com -e admin@example.com,team@example.com -B audit@example.com
python github_org_checker.py my-org --weekly -S
python github_org_checker.py my-org --weekly -S -b   # include the saved BCC list on this send
```

Run `python github_org_checker.py --help` for the full flag reference.

## Configuration

Saved organizations, email settings, and per-organization contributor
history are stored under `~/.github-org-checker/`:

- `organizations.json` — saved orgs and default org
- `email.json` — email sender/recipient/BCC configuration
- `contributors/<org>.json` — seen-contributor history, used by
  `-N/--track-new-contributors`

## Contributing

Contributions are welcome!

- **Bug reports**: [Open an issue](https://github.com/bonelifer/github-org-checker/issues).
- **Everything else** (questions, feature requests, ideas, general discussion): [Use Discussions](https://github.com/bonelifer/github-org-checker/discussions).
- Pull requests are welcome for bug fixes or discussed features.

## Acknowledgments

- Code review, bug fixes, and documentation assisted by [Claude](https://www.anthropic.com/claude).

## License

This project is licensed under the **GNU General Public License v3.0**.

See [LICENSE](LICENSE) for more information.
