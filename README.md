# github-activity-checker

A command-line tool that checks a GitHub organization or personal user
account for recent activity: commits, pull requests, and new repositories.
It can also track issues, releases, PR quality metrics, stale PRs, and
first-time contributors. Reports can be printed to the console, exported to
Markdown, or emailed via `msmtp`.

## Features

- Daily, weekly, monthly, or custom (`N`-day) reporting windows
- Works against both GitHub organizations and personal user accounts,
  detected automatically with no separate flag needed
- Saved targets with a default, so you don't have to pass the org/username
  every run
- When the token belongs to the personal-user target being checked, private
  repositories are included automatically (an org target's private repos
  are already included via the org repos endpoint, provided the token has
  access)
- Per-target org exclusion list, so a personal target's repo list can skip
  orgs it's merely a member of or collaborator on (`-X, --exclude-org`)
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
- A GitHub personal access token is optional but strongly recommended;
  unauthenticated requests are capped at 60/hour

## Installation

```bash
git clone https://github.com/bonelifer/github-activity-checker.git
cd github-activity-checker
pip install -r requirements.txt
```

Set your token as the `GITHUB_TOKEN` environment variable (recommended over
`-t/--token` on the command line, which is visible in shell history and
process listings):

```bash
export GITHUB_TOKEN=your_token_here
```

That only lasts for the current shell session. To make it persist, add it to
your `~/.bashrc` (or `~/.zshrc` if you use zsh) and reload it:

```bash
echo 'export GITHUB_TOKEN=your_token_here' >> ~/.bashrc
source ~/.bashrc
```

## Usage

```bash
# Weekly report for an organization or user
python github_activity_checker.py my-org --weekly
python github_activity_checker.py octocat --weekly

# Save a target as the default so you don't need to pass it again
python github_activity_checker.py -a my-org -s my-org
python github_activity_checker.py --weekly

# Weekly report with issues, releases, and stale-PR detection
python github_activity_checker.py my-org -w -i -I -D 14

# Export to Markdown
python github_activity_checker.py my-org --monthly -x

# Configure and send email
python github_activity_checker.py -c reports@example.com -e admin@example.com,team@example.com -B audit@example.com
python github_activity_checker.py my-org --weekly -S
python github_activity_checker.py my-org --weekly -S -b   # include the saved BCC list on this send
```

## CLI Reference

A **target** is the GitHub organization or username being checked; the tool
auto-detects which one it is.

### Positional

| Argument | Description |
|----------|-------------|
| `target` | GitHub organization or username (optional if a default target is configured) |

### Target Management

| Flag | Description |
|------|-------------|
| `-a, --add-target TARGET` | Add a target to the saved config |
| `-r, --remove-target TARGET` | Remove a target from the saved config |
| `-l, --list-targets` | List all configured targets |
| `-s, --set-default TARGET` | Set the default target |
| `-n, --no-default` | Ignore the saved default target (require an explicit target argument) |

### Org Exclusion

Persisted per target, applies to the resolved target (positional argument
or configured default).

| Flag | Description |
|------|-------------|
| `-X, --exclude-org ORG` | Always exclude this org/owner's repos from the resolved target's repo list (comma-separated for multiple) |
| `--include-org ORG` | Remove an org/owner from the exclusion list (comma-separated for multiple) |
| `--list-excluded-orgs` | List orgs excluded for the resolved target |

### Report Mode

Mutually exclusive; defaults to `--weekly` if none is given.

| Flag | Description |
|------|-------------|
| `-d, --daily` | Today's activity only |
| `-w, --weekly` | Last 7 days |
| `-m, --monthly` | Last 30 days |
| `-C, --custom DAYS` | Last `DAYS` days |

### Output

| Flag | Description |
|------|-------------|
| `-t, --token TOKEN` | GitHub personal access token (falls back to the `GITHUB_TOKEN` env var) |
| `-x, --export-md` | Export the report to a Markdown file |
| `-o, --output FILE` | Output filename for the Markdown export (default: auto-generated) |

### Email Configuration (requires `msmtp`)

| Flag | Description |
|------|-------------|
| `-c, --configure-email EMAIL` | Configure the sender address (e.g. `reports@example.com`) |
| `-e, --email-to LIST` | Comma-separated recipient list |
| `-E, --email-prefix PREFIX` | Email subject prefix (default: `[GitHub Activity]`) |
| `-B, --email-bcc LIST` | Comma-separated BCC list, saved to config. Only sent when `-b/--bcc` is also passed on a given run. Can be set alone (without `-c/--configure-email`) to update just the saved BCC list; pass an empty string to clear it |
| `-S, --send-email` | Send the report via email after generation |
| `-b, --bcc` | Include the saved BCC addresses on this send |

### Optional Extras

Each adds extra GitHub API calls, so enable only what you need.

| Flag | Description |
|------|-------------|
| `-i, --include-issues` | Track issues opened/closed/still-open in the period |
| `-I, --include-releases` | Track new releases/tags published in the period |
| `-p, --pr-metrics` | Time-to-merge, time-to-first-review, and lines changed per PR (2 extra API calls per PR) |
| `-D, --stale-days DAYS` | Flag open PRs idle for `DAYS`+ days (1 extra API call per repo) |
| `-N, --track-new-contributors` | Flag first-time contributors using a locally persisted history file |

Run `python github_activity_checker.py --help` for this same reference from
the command line.

## Configuration

Saved targets, email settings, and per-target contributor history are
stored under `~/.github-activity-checker/`:

- `targets.json` — saved targets and default target
- `email.json` — email sender/recipient/BCC configuration
- `contributors/<target>.json` — seen-contributor history, used by
  `-N/--track-new-contributors`

## Contributing

Contributions are welcome!

- **Bug reports**: [Open an issue](https://github.com/bonelifer/github-activity-checker/issues).
- **Everything else** (questions, feature requests, ideas, general discussion): [Use Discussions](https://github.com/bonelifer/github-activity-checker/discussions).
- Pull requests are welcome for bug fixes or discussed features.

## Acknowledgments

- Code review, bug fixes, and documentation assisted by [Claude](https://www.anthropic.com/claude).

## License

This project is licensed under the **GNU General Public License v3.0**.

See [LICENSE](LICENSE) for more information.
