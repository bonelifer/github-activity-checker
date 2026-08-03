#!/usr/bin/env python3
"""
GitHub Activity Checker

Checks for recent repositories, commits, pull requests, issues, and releases
belonging to a GitHub organization or personal user account. Supports daily,
weekly, monthly, and custom reporting windows, with local config, email
delivery (via msmtp), Markdown export, and several opt-in extras: issue
tracking, release tracking, PR quality metrics, stale-PR detection, and
first-time-contributor tracking.

Inputs:  GitHub organization or username, optional GitHub token (GITHUB_TOKEN
         env var or --token), optional local config file for saved
         targets/email settings.
         To persist GITHUB_TOKEN across shell sessions:
             echo 'export GITHUB_TOKEN=your_token_here' >> ~/.bashrc && source ~/.bashrc
Outputs: Console report, optional Markdown file, optional email.
"""

import os
import sys
import json
import argparse
import email.utils
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from collections import defaultdict
from typing import Dict, List, Optional
from pathlib import Path
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import requests
from dateutil import parser as date_parser


class ConfigManager:
    """Manages target (org/user) configuration, email settings, and contributor history files."""

    CONFIG_DIR = Path.home() / '.github-activity-checker'
    CONFIG_FILE = CONFIG_DIR / 'targets.json'
    EMAIL_CONFIG_FILE = CONFIG_DIR / 'email.json'
    CONTRIBUTORS_DIR = CONFIG_DIR / 'contributors'

    @classmethod
    def ensure_config_dir(cls):
        """Create the config directory if it doesn't exist."""
        cls.CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    @classmethod
    def load_config(cls) -> Dict:
        """Load targets from the config file."""
        cls.ensure_config_dir()

        if not cls.CONFIG_FILE.exists():
            return {'targets': [], 'default_target': None}

        try:
            with open(cls.CONFIG_FILE, 'r') as f:
                return json.load(f)
        except (json.JSONDecodeError, FileNotFoundError):
            return {'targets': [], 'default_target': None}

    @classmethod
    def save_config(cls, config: Dict):
        """Save targets to the config file."""
        cls.ensure_config_dir()

        with open(cls.CONFIG_FILE, 'w') as f:
            json.dump(config, f, indent=2)

    @classmethod
    def add_target(cls, target_name: str, set_as_default: bool = False):
        """Add a target (org or user) to the config."""
        config = cls.load_config()

        if target_name not in config['targets']:
            config['targets'].append(target_name)

        if set_as_default or (config['default_target'] is None and len(config['targets']) == 1):
            config['default_target'] = target_name

        cls.save_config(config)
        print(f"✅ Added target: {target_name}")
        if config['default_target'] == target_name:
            print(f"   Set as default target")

    @classmethod
    def remove_target(cls, target_name: str):
        """Remove a target (org or user) from the config."""
        config = cls.load_config()

        if target_name in config['targets']:
            config['targets'].remove(target_name)

            if config['default_target'] == target_name:
                config['default_target'] = config['targets'][0] if config['targets'] else None

            cls.save_config(config)
            print(f"✅ Removed target: {target_name}")
        else:
            print(f"⚠️  Target not found: {target_name}")

    @classmethod
    def list_targets(cls) -> List[str]:
        """List all configured targets."""
        config = cls.load_config()
        return config.get('targets', [])

    @classmethod
    def get_default_target(cls) -> Optional[str]:
        """Get the default target."""
        config = cls.load_config()
        return config.get('default_target')

    @classmethod
    def set_default_target(cls, target_name: str):
        """Set the default target."""
        config = cls.load_config()

        if target_name not in config['targets']:
            print(f"❌ Target '{target_name}' not found in config. Add it first with: --add-target {target_name}")
            return False

        config['default_target'] = target_name
        cls.save_config(config)
        print(f"✅ Default target set to: {target_name}")
        return True

    @classmethod
    def load_email_config(cls) -> Dict:
        """Load email configuration."""
        cls.ensure_config_dir()

        if not cls.EMAIL_CONFIG_FILE.exists():
            return {}

        try:
            with open(cls.EMAIL_CONFIG_FILE, 'r') as f:
                return json.load(f)
        except (json.JSONDecodeError, FileNotFoundError):
            return {}

    @classmethod
    def save_email_config(cls, email_config: Dict):
        """Save email configuration."""
        cls.ensure_config_dir()

        with open(cls.EMAIL_CONFIG_FILE, 'w') as f:
            json.dump(email_config, f, indent=2)

    @classmethod
    def configure_email(cls, from_addr: str, to_addrs: List[str], subject_prefix: str = None,
                         bcc_addrs: List[str] = None):
        """Configure email settings, including an optional BCC list.

        BCC addresses are stored but only used when a send is explicitly run
        with -b/--bcc, so a saved BCC list doesn't silently apply to every
        future email.
        """
        config = {
            'from': from_addr,
            'to': to_addrs,
            'bcc': bcc_addrs or [],
            'subject_prefix': subject_prefix or '[GitHub Activity]'
        }
        cls.save_email_config(config)
        print(f"✅ Email configured:")
        print(f"   From: {from_addr}")
        print(f"   To: {', '.join(to_addrs)}")
        if bcc_addrs:
            print(f"   BCC (saved, use -b/--bcc to include): {', '.join(bcc_addrs)}")
        print(f"   Subject prefix: {config['subject_prefix']}")

    @classmethod
    def update_email_bcc(cls, bcc_addrs: List[str]) -> bool:
        """Update just the saved BCC list on an existing email config.

        Returns False if no email config exists yet (caller should tell the
        user to run --configure-email first instead).
        """
        config = cls.load_email_config()
        if not config:
            return False

        config['bcc'] = bcc_addrs
        cls.save_email_config(config)
        return True

    @classmethod
    def _contributors_file(cls, target_name: str) -> Path:
        """Return the path to the seen-contributors history file for a target."""
        cls.CONTRIBUTORS_DIR.mkdir(parents=True, exist_ok=True)
        safe_name = target_name.replace('/', '_')
        return cls.CONTRIBUTORS_DIR / f'{safe_name}.json'

    @classmethod
    def load_seen_contributors(cls, target_name: str) -> set:
        """Load the set of contributor names/logins previously seen for a target."""
        path = cls._contributors_file(target_name)

        if not path.exists():
            return set()

        try:
            with open(path, 'r') as f:
                return set(json.load(f))
        except (json.JSONDecodeError, FileNotFoundError):
            return set()

    @classmethod
    def save_seen_contributors(cls, target_name: str, contributors: set):
        """Persist the updated set of seen contributors for a target."""
        path = cls._contributors_file(target_name)

        with open(path, 'w') as f:
            json.dump(sorted(contributors), f, indent=2)


class EmailSender:
    """Handle email sending via msmtp"""

    @staticmethod
    def send_email(subject: str, body: str, html_body: str = None, config: Dict = None,
                    use_bcc: bool = False):
        """Send email using msmtp.

        Args:
            subject: Email subject (appended to the configured subject prefix).
            body: Plain-text message body.
            html_body: Optional HTML message body.
            config: Email config dict (falls back to the saved config file).
            use_bcc: If True, also deliver to any BCC addresses saved in the
                config. BCC addresses are passed only as extra msmtp envelope
                recipients, never added to a message header, so they stay
                blind to the To/Cc recipients.
        """
        if not config:
            config = ConfigManager.load_email_config()

        if not config or not config.get('from') or not config.get('to'):
            print("❌ Email not configured. Run: --configure-email")
            return False

        bcc_addrs = config.get('bcc', []) if use_bcc else []
        if use_bcc and not bcc_addrs:
            print("⚠️  -b/--bcc was set, but no BCC addresses are configured. Run --configure-email with --email-bcc to add some.")

        # Create message
        msg = MIMEMultipart('alternative')
        msg['Subject'] = f"{config.get('subject_prefix', '[GitHub Activity]')} {subject}"
        msg['From'] = config['from']
        msg['To'] = ', '.join(config['to'])
        # Deliberately no 'Bcc' header is set here: BCC recipients are only
        # ever added as extra msmtp command-line (envelope) recipients below,
        # so they remain invisible to everyone else on the message.
        msg['Date'] = email.utils.formatdate(localtime=True)

        # Attach plain text version
        msg.attach(MIMEText(body, 'plain', 'utf-8'))

        # Attach HTML version if provided
        if html_body:
            msg.attach(MIMEText(html_body, 'html', 'utf-8'))

        # Send via msmtp
        try:
            # Convert message to string
            email_content = msg.as_string()

            # Write to temporary file to avoid shell escaping issues
            with tempfile.NamedTemporaryFile(mode='w', suffix='.eml', delete=False, encoding='utf-8') as f:
                f.write(email_content)
                temp_file = f.name

            # Build msmtp command with recipients. BCC addresses are appended
            # here as extra envelope recipients only: msmtp delivers to
            # whatever addresses are passed on the command line regardless of
            # what the message headers say, so this is what makes them blind
            # (no corresponding header was set above).
            cmd = ['msmtp']
            for recipient in config['to']:
                cmd.append(recipient)
            for recipient in bcc_addrs:
                cmd.append(recipient)

            # Read from file and pipe to msmtp
            with open(temp_file, 'r', encoding='utf-8') as f:
                email_data = f.read()

            result = subprocess.run(
                cmd,
                input=email_data,
                text=True,
                capture_output=True
            )

            # Clean up temp file
            os.unlink(temp_file)

            if result.returncode == 0:
                print(f"📧 Email sent successfully to {', '.join(config['to'])}")
                if bcc_addrs:
                    print(f"   (also bcc'd to {', '.join(bcc_addrs)})")
                return True
            else:
                print(f"❌ Failed to send email: {result.stderr}")
                if "unknown command" in result.stderr:
                    print("\n   This error usually means msmtp is receiving malformed email.")
                    print("   Check that your email configuration is correct.")
                return False

        except FileNotFoundError:
            print("❌ msmtp not found. Please install msmtp:")
            print("   Ubuntu/Debian: sudo apt-get install msmtp")
            print("   macOS: brew install msmtp")
            print("   RHEL/CentOS: sudo yum install msmtp")
            return False
        except Exception as e:
            print(f"❌ Error sending email: {e}")
            return False

    @staticmethod
    def generate_html_report(report: Dict) -> str:
        """Generate HTML version of the report for email"""
        html = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="UTF-8">
            <style>
                body {{ font-family: Arial, sans-serif; margin: 0; padding: 20px; background-color: #f5f5f5; }}
                .container {{ max-width: 900px; margin: 0 auto; background-color: white; padding: 20px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }}
                h1 {{ color: #24292e; border-bottom: 2px solid #e1e4e8; padding-bottom: 10px; }}
                h2 {{ color: #24292e; margin-top: 25px; }}
                h3 {{ color: #24292e; margin-top: 20px; }}
                .summary {{ background-color: #f6f8fa; padding: 15px; border-radius: 6px; margin: 20px 0; }}
                .summary-item {{ display: inline-block; margin-right: 30px; }}
                .summary-number {{ font-size: 24px; font-weight: bold; color: #0366d6; }}
                .repo {{ border-left: 3px solid #0366d6; padding-left: 15px; margin: 15px 0; }}
                .commit {{ font-family: monospace; font-size: 14px; color: #586069; margin: 5px 0; }}
                .pr {{ background-color: #f1f8ff; padding: 8px; margin: 8px 0; border-radius: 4px; font-size: 14px; }}
                .badge {{ display: inline-block; padding: 2px 6px; font-size: 12px; border-radius: 12px; }}
                .badge-open {{ background-color: #2cbe4e; color: white; }}
                .badge-closed {{ background-color: #cb2431; color: white; }}
                .footer {{ margin-top: 30px; padding-top: 15px; border-top: 1px solid #e1e4e8; font-size: 12px; color: #586069; text-align: center; }}
                table {{ width: 100%; border-collapse: collapse; }}
                th, td {{ padding: 8px; text-align: left; border-bottom: 1px solid #e1e4e8; }}
                th {{ background-color: #f6f8fa; }}
            </style>
        </head>
        <body>
            <div class="container">
                <h1>📊 {report['mode'].upper()} Activity Report: {report['target']}</h1>
                <p><strong>Period:</strong> {report['period_name']}<br>
                <strong>From:</strong> {datetime.fromisoformat(report['since_date']).strftime('%Y-%m-%d %H:%M:%S')} UTC<br>
                <strong>To:</strong> {datetime.fromisoformat(report['until_date']).strftime('%Y-%m-%d %H:%M:%S')} UTC</p>
        """

        # Executive Summary
        html += f"""
                <div class="summary">
                    <h3 style="margin-top: 0;">Executive Summary</h3>
                    <div class="summary-item"><span class="summary-number">{report['summary']['active_repos']}</span><br>Active Repos</div>
                    <div class="summary-item"><span class="summary-number">{report['summary']['total_commits']}</span><br>Commits</div>
                    <div class="summary-item"><span class="summary-number">{report['summary']['total_prs']}</span><br>PRs</div>
                    <div class="summary-item"><span class="summary-number">{len(report['summary']['unique_contributors'])}</span><br>Contributors</div>
                    <div class="summary-item"><span class="summary-number">{len(report['new_repos'])}</span><br>New Repos</div>
                </div>
        """

        # Top Contributors (commit authors)
        if report['top_contributors'] and report['mode'] in ['weekly', 'monthly']:
            html += """
                <h2>🏆 Top Contributors</h2>
                <table>
                    <tr><th>Contributor</th><th>Commits</th></tr>
            """
            for contributor, count in sorted_contributors(report['top_contributors'], limit=10):
                html += f"<tr><td>{contributor}</td><td>{count}</td></tr>"
            html += "</table>"

        # PR Contributors (heads-up reference, not a ranking)
        if report.get('pr_contributors'):
            html += """
                <h2>👤 PR Contributors</h2>
                <table>
                    <tr><th>Author</th><th>PRs</th></tr>
            """
            for contributor, count in sorted_pr_contributors(report['pr_contributors']):
                html += f"<tr><td>{contributor}</td><td>{count}</td></tr>"
            html += "</table>"

        # Daily Breakdown
        if report['daily_breakdown']:
            html += """
                <h2>📊 Daily Activity Breakdown</h2>
                <table>
                    <tr><th>Date</th><th>Commits</th><th>PRs</th><th>Total</th></tr>
            """
            sorted_days = sorted(report['daily_breakdown'].items())
            for day, counts in sorted_days:
                total = counts['commits'] + counts['prs']
                html += f"<tr><td>{day}</td><td>{counts['commits']}</td><td>{counts['prs']}</td><td>{total}</td></tr>"
            html += "</table>"

        # New Repositories
        if report['new_repos']:
            html += "<h2>🆕 New Repositories</h2><ul>"
            for repo in report['new_repos'][:10]:
                created = date_parser.parse(repo['created_at']).strftime('%Y-%m-%d')
                html += f"<li><strong><a href='{repo['html_url']}'>{repo['full_name']}</a></strong> - Created {created}</li>"
            html += "</ul>"

        # New Releases
        if report.get('new_releases'):
            html += "<h2>🏷️ New Releases</h2><ul>"
            for release in report['new_releases']:
                published = date_parser.parse(release['published_at']).strftime('%Y-%m-%d')
                html += f"<li><strong>{release['repo']}</strong> — <a href='{release['url']}'>{release['name']}</a> ({release['tag_name']}) - {published}</li>"
            html += "</ul>"

        # Issue Activity
        if report.get('issue_activity') is not None:
            html += f"""
                <h2>🐛 Issue Activity</h2>
                <p>Opened: {report['summary']['total_issues_opened']} |
                   Closed: {report['summary']['total_issues_closed']} |
                   Currently open: {report['summary']['currently_open_issues']}</p>
            """
            if report['issue_activity']:
                html += """
                <table>
                    <tr><th>Repository</th><th>Opened</th><th>Closed</th></tr>
                """
                for repo_name, issue_summary in report['issue_activity'].items():
                    html += f"<tr><td>{repo_name}</td><td>{issue_summary['opened_count']}</td><td>{issue_summary['closed_count']}</td></tr>"
                html += "</table>"

        # PR Quality Metrics
        if report.get('pr_metrics'):
            metrics_summary = pr_metrics_summary(report['pr_metrics'])

            html += "<h2>⏱️ PR Quality Metrics</h2><ul>"
            if metrics_summary['avg_merge_hours'] is not None:
                html += f"<li>Avg time to merge: {metrics_summary['avg_merge_hours']:.1f}h</li>"
            if metrics_summary['avg_review_hours'] is not None:
                html += f"<li>Avg time to first review: {metrics_summary['avg_review_hours']:.1f}h</li>"
            html += f"<li>Lines changed: +{metrics_summary['total_additions']} / -{metrics_summary['total_deletions']}</li>"
            html += "</ul>"

        # Stale PRs
        if report.get('stale_prs'):
            html += f"<h2>⚠️ Stale Pull Requests ({report.get('stale_days', '?')}+ days idle)</h2><ul>"
            for pr in sorted(report['stale_prs'], key=lambda x: -x['days_idle']):
                html += f"<li><strong>{pr['repo']}</strong> — <a href='{pr['url']}'>#{pr['number']}</a> {pr['title']} ({pr['author']}, idle {pr['days_idle']}d)</li>"
            html += "</ul>"

        # First-time Contributors
        if report.get('first_time_contributors'):
            html += "<h2>✨ First-Time Contributors</h2><ul>"
            for name in report['first_time_contributors']:
                html += f"<li>{name}</li>"
            html += "</ul>"

        # Repository Activity
        if report['repo_activity']:
            html += "<h2>📈 Repository Activity</h2>"

            for repo_name, activity in sorted_repo_activity(report['repo_activity'])[:15]:
                html += f"""
                <div class="repo">
                    <h3><a href='{activity['repo_url']}'>{repo_name}</a></h3>
                    <p><strong>Commits:</strong> {activity['commit_count']} | <strong>PRs:</strong> {activity['pr_count']}</p>
                """

                if activity['commits']:
                    html += "<p><strong>Recent commits:</strong></p><ul>"
                    for commit in activity['commits'][:3]:
                        commit_date = datetime.fromisoformat(commit['date']).strftime('%Y-%m-%d %H:%M')
                        html += f"<li class='commit'><a href='{commit['url']}'>{commit['sha']}</a> - {commit['message']}<br><small>{commit['author']} @ {commit_date}</small></li>"
                    html += "</ul>"

                if activity['pull_requests']:
                    html += "<p><strong>Active PRs:</strong></p>"
                    for pr in activity['pull_requests'][:3]:
                        state_class = "badge-open" if pr['state'] == 'open' else "badge-closed"
                        html += f"""
                        <div class='pr'>
                            <span class='badge {state_class}'>{pr['state']}</span>
                            <strong><a href='{pr['url']}'>#{pr['number']}</a></strong> - {pr['title']}<br>
                            <small>by {pr['author']}</small>
                        </div>
                        """

                html += "</div>"

        # Footer
        html += f"""
                <div class="footer">
                    Report generated on {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC
                </div>
            </div>
        </body>
        </html>
        """

        return html


def sorted_contributors(contributors: Dict[str, int], limit: int = None) -> List:
    """Sort a contributor->count dict by count descending, used for the commit-author ranking."""
    items = sorted(contributors.items(), key=lambda x: x[1], reverse=True)
    return items[:limit] if limit else items


def sorted_pr_contributors(pr_contributors: Dict[str, int]) -> List:
    """Sort PR authors by count desc, then name asc. A heads-up reference, not a ranking."""
    return sorted(pr_contributors.items(), key=lambda x: (-x[1], x[0]))


def sorted_repo_activity(repo_activity: Dict) -> List:
    """Sort repos by combined commit+PR count, descending."""
    return sorted(repo_activity.items(), key=lambda x: x[1]['commit_count'] + x[1]['pr_count'], reverse=True)


def pr_metrics_summary(pr_metrics: List[Dict]) -> Dict:
    """Aggregate PR quality metrics into averages and totals for display."""
    merge_times = [m['time_to_merge_hours'] for m in pr_metrics if m['time_to_merge_hours'] is not None]
    review_times = [m['time_to_first_review_hours'] for m in pr_metrics if m['time_to_first_review_hours'] is not None]
    return {
        'avg_merge_hours': sum(merge_times) / len(merge_times) if merge_times else None,
        'avg_review_hours': sum(review_times) / len(review_times) if review_times else None,
        'total_additions': sum(m['additions'] for m in pr_metrics if m['additions'] is not None),
        'total_deletions': sum(m['deletions'] for m in pr_metrics if m['deletions'] is not None),
    }


class GitHubActivityChecker:
    """Fetches and summarizes GitHub activity for an organization or user over a configurable window."""

    def __init__(
        self,
        target: str,
        token: str = None,
        mode: str = 'weekly',
        custom_days: int = None,
        include_issues: bool = False,
        include_releases: bool = False,
        pr_metrics: bool = False,
        stale_days: Optional[int] = None,
        track_new_contributors: bool = False,
    ):
        """
        Initialize the checker with a specific mode and optional extras.

        Args:
            target: GitHub organization name or username.
            token: GitHub personal access token (falls back to GITHUB_TOKEN env var).
            mode: 'daily', 'weekly', 'monthly', or 'custom'.
            custom_days: Number of days for custom mode.
            include_issues: Track issues opened/closed/still-open in the period.
            include_releases: Track new releases/tags published in the period.
            pr_metrics: Collect time-to-merge, time-to-first-review, and lines
                changed for PRs (adds two extra API calls per PR).
            stale_days: If set, flag open PRs idle for at least this many days
                (adds one extra API call per repo).
            track_new_contributors: Diff this run's contributors against a
                locally persisted history file and flag first-timers.
        """
        self.target = target
        self.token = token or os.getenv('GITHUB_TOKEN')
        self.mode = mode
        self.base_url = "https://api.github.com"
        self.headers = {
            "Accept": "application/vnd.github.v3+json"
        }
        if self.token:
            self.headers["Authorization"] = f"token {self.token}"

        self.include_issues = include_issues
        self.include_releases = include_releases
        self.pr_metrics = pr_metrics
        self.stale_days = stale_days
        self.track_new_contributors = track_new_contributors

        now = datetime.now(timezone.utc)

        # Set date ranges based on mode
        if mode == 'daily':
            # Today from 00:00:00 UTC
            self.since_date = datetime(now.year, now.month, now.day, 0, 0, 0, tzinfo=timezone.utc)
            self.until_date = now
            self.period_name = "Today"
            self.period_days = 1

        elif mode == 'weekly':
            # Last 7 days (current day - 7 days)
            self.since_date = now - timedelta(days=7)
            self.until_date = now
            self.period_name = "Last 7 Days"
            self.period_days = 7

        elif mode == 'monthly':
            # Last 30 days
            self.since_date = now - timedelta(days=30)
            self.until_date = now
            self.period_name = "Last 30 Days"
            self.period_days = 30

        elif mode == 'custom':
            self.period_days = custom_days or 7
            self.since_date = now - timedelta(days=self.period_days)
            self.until_date = now
            self.period_name = f"Last {self.period_days} Days"

        else:
            raise ValueError(f"Unknown mode: {mode}")

        # For weekly/monthly summaries, track activity by day
        self.track_daily_activity = mode in ['weekly', 'monthly']

    def _make_request(self, url: str, params: Dict = None) -> List[Dict]:
        """Make paginated GitHub API request"""
        all_data = []
        page = 1
        per_page = 100

        while True:
            if params is None:
                params = {}
            params.update({'page': page, 'per_page': per_page})

            response = requests.get(url, headers=self.headers, params=params)

            if response.status_code == 403 and 'rate limit' in response.text.lower():
                print("⚠️  Rate limit exceeded. Please wait or use a token with higher limits.")
                break

            if response.status_code != 200:
                print(f"❌ API error {response.status_code}: {response.text}")
                break

            data = response.json()
            if not data:
                break

            all_data.extend(data)
            page += 1

            if len(data) < per_page:
                break

        return all_data

    def get_account_type(self) -> str:
        """Probe whether target is a GitHub organization or personal user account.

        /users/{name} works for both account types, unlike /orgs/{name}/repos
        which 404s for personal accounts, so this determines which repos
        endpoint to use. Cached after the first call. Defaults to
        'Organization' if the probe fails, so a genuinely nonexistent account
        still surfaces a normal 404 from the repos call.
        """
        if not hasattr(self, '_account_type'):
            response = requests.get(f"{self.base_url}/users/{self.target}", headers=self.headers)
            self._account_type = response.json().get('type', 'Organization') if response.status_code == 200 else 'Organization'
        return self._account_type

    def get_repositories(self) -> List[Dict]:
        """Get all repositories for the target organization or user account"""
        endpoint = 'users' if self.get_account_type() == 'User' else 'orgs'
        url = f"{self.base_url}/{endpoint}/{self.target}/repos"
        params = {'sort': 'updated', 'direction': 'desc'}
        return self._make_request(url, params)

    def get_recent_repos(self, repos: List[Dict]) -> List[Dict]:
        """Filter repositories created within the date range"""
        recent = []
        for repo in repos:
            created_at = date_parser.parse(repo['created_at'])
            if created_at >= self.since_date and created_at <= self.until_date:
                recent.append(repo)
        return recent

    def get_commits_for_repo(self, repo_full_name: str) -> List[Dict]:
        """Get recent commits for a repository"""
        url = f"{self.base_url}/repos/{repo_full_name}/commits"
        params = {
            'since': self.since_date.isoformat(),
            'until': self.until_date.isoformat()
        }
        return self._make_request(url, params)

    def get_pull_requests_for_repo(self, repo_full_name: str) -> List[Dict]:
        """Get pull requests updated within the date range"""
        url = f"{self.base_url}/repos/{repo_full_name}/pulls"
        params = {
            'state': 'all',
            'sort': 'updated',
            'direction': 'desc',
            'per_page': 100
        }

        prs = self._make_request(url, params)

        # Filter PRs updated within our date range
        filtered_prs = []
        for pr in prs:
            updated_at = date_parser.parse(pr['updated_at'])
            if updated_at >= self.since_date and updated_at <= self.until_date:
                filtered_prs.append(pr)

        return filtered_prs

    def get_issues_for_repo(self, repo_full_name: str) -> List[Dict]:
        """Get issues (excluding pull requests) updated within the date range.

        The GitHub issues endpoint also returns pull requests, since PRs are
        a superset of issues internally; entries with a 'pull_request' key
        are filtered out here to leave only true issues.
        """
        url = f"{self.base_url}/repos/{repo_full_name}/issues"
        params = {
            'state': 'all',
            'since': self.since_date.isoformat(),
            'sort': 'updated',
            'direction': 'desc',
        }
        raw_issues = self._make_request(url, params)
        issues = [issue for issue in raw_issues if 'pull_request' not in issue]

        filtered = []
        for issue in issues:
            updated_at = date_parser.parse(issue['updated_at'])
            if updated_at <= self.until_date:
                filtered.append(issue)
        return filtered

    def get_releases_for_repo(self, repo_full_name: str) -> List[Dict]:
        """Get releases published within the date range.

        The releases endpoint has no since/until filter, so all releases are
        fetched and filtered locally by published_at (falling back to
        created_at for draft releases without a publish date).
        """
        url = f"{self.base_url}/repos/{repo_full_name}/releases"
        releases = self._make_request(url)

        recent = []
        for release in releases:
            published_at = release.get('published_at') or release.get('created_at')
            if not published_at:
                continue
            published_date = date_parser.parse(published_at)
            if self.since_date <= published_date <= self.until_date:
                recent.append(release)
        return recent

    def get_pr_detail(self, repo_full_name: str, pr_number: int) -> Optional[Dict]:
        """Fetch full PR detail. The additions/deletions fields are only
        present on the single-PR endpoint, not the list endpoint."""
        url = f"{self.base_url}/repos/{repo_full_name}/pulls/{pr_number}"
        response = requests.get(url, headers=self.headers)
        if response.status_code == 200:
            return response.json()
        return None

    def get_first_review_date(self, repo_full_name: str, pr_number: int) -> Optional[datetime]:
        """Fetch the timestamp of the earliest submitted review on a PR, if any."""
        url = f"{self.base_url}/repos/{repo_full_name}/pulls/{pr_number}/reviews"
        reviews = self._make_request(url)
        if not reviews:
            return None
        review_dates = [date_parser.parse(r['submitted_at']) for r in reviews if r.get('submitted_at')]
        return min(review_dates) if review_dates else None

    def get_stale_open_prs(self, repo_full_name: str, stale_days: int) -> List[Dict]:
        """Get open PRs that have had no activity for at least stale_days days.

        This looks at all currently-open PRs regardless of the report's date
        window, since staleness is defined by the absence of recent activity.
        """
        url = f"{self.base_url}/repos/{repo_full_name}/pulls"
        params = {
            'state': 'open',
            'sort': 'updated',
            'direction': 'asc',
            'per_page': 100,
        }
        open_prs = self._make_request(url, params)

        threshold = datetime.now(timezone.utc) - timedelta(days=stale_days)
        stale = []
        for pr in open_prs:
            updated_at = date_parser.parse(pr['updated_at'])
            if updated_at <= threshold:
                stale.append(pr)
        return stale

    def _process_repo_issues(self, repo_name: str) -> Dict:
        """Summarize issue activity (opened, closed, still open) for one repo."""
        issues = self.get_issues_for_repo(repo_name)
        opened_count = 0
        closed_count = 0
        still_open_count = 0

        for issue in issues:
            created_at = date_parser.parse(issue['created_at'])
            if self.since_date <= created_at <= self.until_date:
                opened_count += 1

            if issue['state'] == 'closed' and issue.get('closed_at'):
                closed_at = date_parser.parse(issue['closed_at'])
                if self.since_date <= closed_at <= self.until_date:
                    closed_count += 1
            elif issue['state'] == 'open':
                still_open_count += 1

        return {
            'opened_count': opened_count,
            'closed_count': closed_count,
            'still_open_count': still_open_count,
        }

    def _process_repo_releases(self, repo_name: str) -> List[Dict]:
        """Collect new releases/tags for one repo within the date range."""
        releases = self.get_releases_for_repo(repo_name)
        result = []
        for release in releases:
            published_at = release.get('published_at') or release.get('created_at')
            changelog = (release.get('body') or '').strip()
            result.append({
                'repo': repo_name,
                'tag_name': release.get('tag_name', ''),
                'name': release.get('name') or release.get('tag_name', ''),
                'url': release['html_url'],
                'published_at': published_at,
                'changelog': changelog[:300],
            })
        return result

    def _collect_pr_metrics(self, repo_name: str, pr: Dict) -> Dict:
        """Gather quality metrics for a single pull request.

        Costs two extra API calls per PR (reviews + single-PR detail), so
        this is only invoked when --pr-metrics is explicitly enabled.
        """
        metrics = {
            'repo': repo_name,
            'number': pr['number'],
            'title': pr['title'][:80],
            'time_to_merge_hours': None,
            'time_to_first_review_hours': None,
            'additions': None,
            'deletions': None,
        }

        created_at = date_parser.parse(pr['created_at'])

        if pr.get('merged_at'):
            merged_at = date_parser.parse(pr['merged_at'])
            metrics['time_to_merge_hours'] = round((merged_at - created_at).total_seconds() / 3600, 1)

        first_review = self.get_first_review_date(repo_name, pr['number'])
        if first_review:
            metrics['time_to_first_review_hours'] = round((first_review - created_at).total_seconds() / 3600, 1)

        detail = self.get_pr_detail(repo_name, pr['number'])
        if detail:
            metrics['additions'] = detail.get('additions')
            metrics['deletions'] = detail.get('deletions')

        return metrics

    def _process_stale_prs(self, repo_name: str) -> List[Dict]:
        """Flag open PRs with no activity for at least self.stale_days days."""
        stale = self.get_stale_open_prs(repo_name, self.stale_days)
        now = datetime.now(timezone.utc)
        result = []
        for pr in stale:
            updated_at = date_parser.parse(pr['updated_at'])
            days_idle = (now - updated_at).days
            result.append({
                'repo': repo_name,
                'number': pr['number'],
                'title': pr['title'][:80],
                'author': pr['user']['login'],
                'url': pr['html_url'],
                'days_idle': days_idle,
            })
        return result

    def _process_repo_commits(self, repo_name: str, activity_report: Dict) -> List[Dict]:
        """Fetch and record commits for one repo, updating shared report accumulators."""
        commits = self.get_commits_for_repo(repo_name)
        recent_commits = []

        for commit in commits:
            commit_date = date_parser.parse(commit['commit']['author']['date'])
            if commit_date >= self.since_date and commit_date <= self.until_date:
                author = commit['commit']['author']['name']
                commit_info = {
                    'sha': commit['sha'][:8],
                    'message': commit['commit']['message'].split('\n')[0][:60],
                    'author': author,
                    'author_login': commit.get('author', {}).get('login', author),
                    'author_email': commit['commit']['author']['email'],
                    'date': commit_date.isoformat(),
                    'url': commit['html_url']
                }
                recent_commits.append(commit_info)

                activity_report['summary']['total_commits'] += 1
                activity_report['summary']['unique_contributors'].add(author)
                activity_report['top_contributors'][author] += 1

                if self.track_daily_activity:
                    day_key = commit_date.strftime('%Y-%m-%d')
                    activity_report['daily_breakdown'][day_key]['commits'] += 1

        return recent_commits

    def _process_repo_prs(self, repo_name: str, activity_report: Dict) -> List[Dict]:
        """Fetch and record PRs for one repo, updating shared report accumulators."""
        prs = self.get_pull_requests_for_repo(repo_name)
        recent_prs = []

        for pr in prs:
            pr_updated = date_parser.parse(pr['updated_at'])
            if pr_updated >= self.since_date and pr_updated <= self.until_date:
                pr_info = {
                    'number': pr['number'],
                    'title': pr['title'][:80],
                    'state': pr['state'],
                    'author': pr['user']['login'],
                    'created': pr['created_at'],
                    'updated': pr['updated_at'],
                    'url': pr['html_url'],
                    'commits_url': pr['commits_url']
                }
                recent_prs.append(pr_info)

                activity_report['summary']['total_prs'] += 1
                activity_report['pr_contributors'][pr_info['author']] += 1

                # Use the already-parsed pr_updated datetime here rather than
                # re-parsing a nonexistent 'updated' key on the raw API object
                # (the raw PR object only has 'updated_at').
                if self.track_daily_activity:
                    day_key = pr_updated.strftime('%Y-%m-%d')
                    activity_report['daily_breakdown'][day_key]['prs'] += 1

                if self.pr_metrics:
                    activity_report['pr_metrics'].append(self._collect_pr_metrics(repo_name, pr))

        return recent_prs

    def check_activity(self) -> Dict:
        """Main method to check all activity"""
        account_type = self.get_account_type()
        print(f"🔍 Checking {'user' if account_type == 'User' else 'organization'}: {self.target}")
        print(f"📅 Period: {self.period_name}")
        print(f"⏰ From: {self.since_date.strftime('%Y-%m-%d %H:%M:%S')} UTC")
        print(f"⏰ To:   {self.until_date.strftime('%Y-%m-%d %H:%M:%S')} UTC")
        extras = []
        if self.include_issues:
            extras.append("issues")
        if self.include_releases:
            extras.append("releases")
        if self.pr_metrics:
            extras.append("PR metrics")
        if self.stale_days is not None:
            extras.append(f"stale PRs ({self.stale_days}+ days)")
        if self.track_new_contributors:
            extras.append("first-time contributors")
        if extras:
            print(f"➕ Extras enabled: {', '.join(extras)}")
        print("-" * 60)

        # Get all repositories
        repos = self.get_repositories()
        print(f"📚 Total repositories found: {len(repos)}")

        # Find repositories created in the period
        recent_repos = self.get_recent_repos(repos)
        print(f"🆕 Repositories created: {len(recent_repos)}")

        # Initialize activity report
        activity_report = {
            'target': self.target,
            'mode': self.mode,
            'period_name': self.period_name,
            'since_date': self.since_date.isoformat(),
            'until_date': self.until_date.isoformat(),
            'period_days': self.period_days,
            'total_repos': len(repos),
            'new_repos': recent_repos,
            'repo_activity': defaultdict(dict),
            'daily_breakdown': defaultdict(lambda: {'commits': 0, 'prs': 0}) if self.track_daily_activity else None,
            'top_contributors': defaultdict(int),
            'pr_contributors': defaultdict(int),
            'summary': {
                'total_commits': 0,
                'total_prs': 0,
                'active_repos': 0,
                'unique_contributors': set()
            }
        }

        if self.include_issues:
            activity_report['issue_activity'] = {}
            activity_report['summary']['total_issues_opened'] = 0
            activity_report['summary']['total_issues_closed'] = 0
            activity_report['summary']['currently_open_issues'] = 0

        if self.include_releases:
            activity_report['new_releases'] = []

        if self.pr_metrics:
            activity_report['pr_metrics'] = []

        if self.stale_days is not None:
            activity_report['stale_prs'] = []
            activity_report['stale_days'] = self.stale_days

        # Check each repository for recent commits and PRs (plus any enabled extras)
        for idx, repo in enumerate(repos, 1):
            repo_name = repo['full_name']
            print(f"\n📁 [{idx}/{len(repos)}] Checking: {repo_name}")

            recent_commits = self._process_repo_commits(repo_name, activity_report)
            recent_prs = self._process_repo_prs(repo_name, activity_report)

            if self.include_issues:
                issue_summary = self._process_repo_issues(repo_name)
                if issue_summary['opened_count'] or issue_summary['closed_count'] or issue_summary['still_open_count']:
                    activity_report['issue_activity'][repo_name] = issue_summary
                activity_report['summary']['total_issues_opened'] += issue_summary['opened_count']
                activity_report['summary']['total_issues_closed'] += issue_summary['closed_count']
                activity_report['summary']['currently_open_issues'] += issue_summary['still_open_count']

            if self.include_releases:
                activity_report['new_releases'].extend(self._process_repo_releases(repo_name))

            if self.stale_days is not None:
                activity_report['stale_prs'].extend(self._process_stale_prs(repo_name))

            if recent_commits or recent_prs:
                activity_report['repo_activity'][repo_name] = {
                    'commits': recent_commits,
                    'pull_requests': recent_prs,
                    'repo_url': repo['html_url'],
                    'description': repo['description'],
                    'commit_count': len(recent_commits),
                    'pr_count': len(recent_prs)
                }
                activity_report['summary']['active_repos'] += 1
                print(f"  ✅ Commits: {len(recent_commits)} | PRs: {len(recent_prs)}")
            else:
                print(f"  ⚪ No activity in period")

        if self.track_new_contributors:
            current_contributors = set(activity_report['summary']['unique_contributors']) | set(activity_report['pr_contributors'].keys())
            previously_seen = ConfigManager.load_seen_contributors(self.target)
            activity_report['first_time_contributors'] = sorted(current_contributors - previously_seen)
            ConfigManager.save_seen_contributors(self.target, previously_seen | current_contributors)

        # Convert set to list for JSON serialization
        activity_report['summary']['unique_contributors'] = list(activity_report['summary']['unique_contributors'])

        return activity_report

    def print_report(self, report: Dict):
        """Print formatted activity report based on mode"""
        print("\n" + "="*80)

        # Header based on mode
        mode_icons = {
            'daily': '📅',
            'weekly': '📆',
            'monthly': '📊',
            'custom': '📈'
        }
        icon = mode_icons.get(self.mode, '📊')

        if self.mode == 'daily':
            date_str = datetime.fromisoformat(report['since_date']).strftime('%Y-%m-%d')
            print(f"{icon} DAILY ACTIVITY REPORT - {report['target']} - {date_str}")
        elif self.mode == 'weekly':
            start = datetime.fromisoformat(report['since_date']).strftime('%Y-%m-%d')
            end = datetime.fromisoformat(report['until_date']).strftime('%Y-%m-%d')
            print(f"{icon} WEEKLY ACTIVITY REPORT - {report['target']} ({start} to {end})")
        elif self.mode == 'monthly':
            start = datetime.fromisoformat(report['since_date']).strftime('%Y-%m-%d')
            end = datetime.fromisoformat(report['until_date']).strftime('%Y-%m-%d')
            print(f"{icon} MONTHLY ACTIVITY REPORT - {report['target']} ({start} to {end})")
        else:
            print(f"{icon} ACTIVITY REPORT - {report['target']} - {report['period_name']}")

        print("="*80)

        # Executive Summary
        print(f"\n📋 EXECUTIVE SUMMARY")
        print("-" * 40)
        print(f"  • Active repositories: {report['summary']['active_repos']}/{report['total_repos']}")
        print(f"  • Total commits: {report['summary']['total_commits']}")
        print(f"  • Total pull requests: {report['summary']['total_prs']}")
        print(f"  • Unique contributors: {len(report['summary']['unique_contributors'])}")
        print(f"  • New repositories: {len(report['new_repos'])}")
        if report.get('issue_activity') is not None:
            print(f"  • Issues opened/closed: {report['summary']['total_issues_opened']}/{report['summary']['total_issues_closed']}")
        if report.get('new_releases') is not None:
            print(f"  • New releases: {len(report['new_releases'])}")

        # Top contributors (for weekly/monthly)
        if report['top_contributors'] and self.mode in ['weekly', 'monthly']:
            print(f"\n🏆 TOP CONTRIBUTORS")
            print("-" * 40)
            for idx, (contributor, count) in enumerate(sorted_contributors(report['top_contributors'], limit=5), 1):
                medal = {1: '🥇', 2: '🥈', 3: '🥉'}.get(idx, '  ')
                print(f"  {medal} {contributor}: {count} commits")

        # PR contributors: a heads-up reference of who opened PRs, not a ranking
        if report.get('pr_contributors'):
            print(f"\n👤 PR CONTRIBUTORS")
            print("-" * 40)
            for contributor, count in sorted_pr_contributors(report['pr_contributors']):
                print(f"  • {contributor} ({count})")

        # Daily breakdown (for weekly/monthly)
        if report['daily_breakdown'] and self.mode in ['weekly', 'monthly']:
            print(f"\n📊 DAILY BREAKDOWN")
            print("-" * 40)
            sorted_days = sorted(report['daily_breakdown'].items())
            for day, counts in sorted_days:
                day_name = datetime.strptime(day, '%Y-%m-%d').strftime('%a, %b %d')
                bar_len = min(30, counts['commits'] + counts['prs'])
                bar = "█" * bar_len if bar_len > 0 else "·"
                print(f"  {day_name:12} {bar} {counts['commits']} commits, {counts['prs']} PRs")

        # New repositories
        if report['new_repos']:
            print(f"\n🆕 NEW REPOSITORIES")
            print("-" * 40)
            for repo in report['new_repos'][:10]:
                created = date_parser.parse(repo['created_at']).strftime('%Y-%m-%d')
                print(f"  • {repo['full_name']}")
                print(f"    Created: {created} | URL: {repo['html_url']}")
                if repo['description'] and len(repo['description']) < 80:
                    print(f"    {repo['description']}")
                print()
            if len(report['new_repos']) > 10:
                print(f"  ... and {len(report['new_repos']) - 10} more new repositories\n")

        # New releases/tags
        if report.get('new_releases'):
            print(f"\n🏷️  NEW RELEASES")
            print("-" * 40)
            for release in report['new_releases']:
                published = date_parser.parse(release['published_at']).strftime('%Y-%m-%d')
                print(f"  • {release['repo']} — {release['name']} ({release['tag_name']}) - {published}")
                print(f"    {release['url']}")

        # Issue activity
        if report.get('issue_activity') is not None:
            print(f"\n🐛 ISSUE ACTIVITY")
            print("-" * 40)
            print(f"  • Opened: {report['summary']['total_issues_opened']}")
            print(f"  • Closed: {report['summary']['total_issues_closed']}")
            print(f"  • Currently open (touched this period): {report['summary']['currently_open_issues']}")
            for repo_name, issue_summary in report['issue_activity'].items():
                if issue_summary['opened_count'] or issue_summary['closed_count']:
                    print(f"    📦 {repo_name}: {issue_summary['opened_count']} opened, {issue_summary['closed_count']} closed")

        # PR quality metrics
        if report.get('pr_metrics'):
            print(f"\n⏱️  PR QUALITY METRICS")
            print("-" * 40)
            metrics_summary = pr_metrics_summary(report['pr_metrics'])
            if metrics_summary['avg_merge_hours'] is not None:
                print(f"  • Avg time to merge: {metrics_summary['avg_merge_hours']:.1f}h")
            if metrics_summary['avg_review_hours'] is not None:
                print(f"  • Avg time to first review: {metrics_summary['avg_review_hours']:.1f}h")
            print(f"  • Lines changed: +{metrics_summary['total_additions']} / -{metrics_summary['total_deletions']}")

        # Stale PRs
        if report.get('stale_prs'):
            print(f"\n⚠️  STALE PULL REQUESTS (idle {report.get('stale_days', '?')}+ days)")
            print("-" * 40)
            for pr in sorted(report['stale_prs'], key=lambda x: -x['days_idle']):
                print(f"  • {pr['repo']} #{pr['number']}: {pr['title']} ({pr['author']}, idle {pr['days_idle']}d)")

        # First-time contributors
        if report.get('first_time_contributors'):
            print(f"\n✨ FIRST-TIME CONTRIBUTORS")
            print("-" * 40)
            for name in report['first_time_contributors']:
                print(f"  • {name}")

        # Activity by repository
        if report['repo_activity']:
            if self.mode == 'daily':
                print(f"\n📈 TODAY'S ACTIVITY")
            elif self.mode == 'weekly':
                print(f"\n📈 WEEKLY ACTIVITY BY REPOSITORY")
            elif self.mode == 'monthly':
                print(f"\n📈 MONTHLY ACTIVITY BY REPOSITORY")
            else:
                print(f"\n📈 ACTIVITY BY REPOSITORY")
            print("-" * 40)

            sorted_repos = sorted_repo_activity(report['repo_activity'])

            for repo_name, activity in sorted_repos[:15]:
                print(f"\n📦 {repo_name}")
                if activity['description']:
                    desc = activity['description'][:60]
                    print(f"   📝 {desc}")

                print(f"   💻 Commits: {activity['commit_count']} | 🔀 PRs: {activity['pr_count']}")

                sample_limit = 5 if self.mode == 'daily' else 3
                if activity['commits']:
                    print(f"   Recent commits:")
                    for commit in activity['commits'][:sample_limit]:
                        commit_time = date_parser.parse(commit['date']).strftime('%H:%M' if self.mode == 'daily' else '%m/%d %H:%M')
                        print(f"      • {commit['sha']} - {commit['message'][:50]}")
                        print(f"        {commit['author']} @ {commit_time}")

                if activity['pull_requests']:
                    print(f"   Active PRs:")
                    for pr in activity['pull_requests'][:sample_limit]:
                        print(f"      • #{pr['number']}: {pr['title'][:50]}")
                        print(f"        {pr['author']} | {pr['state']}")

            if len(sorted_repos) > 15:
                print(f"\n   ... and {len(sorted_repos) - 15} more repositories with activity")

        elif self.mode == 'daily':
            print("\n✅ No activity detected today! Everything is quiet.")
        else:
            print("\n⚠️  No activity detected in this period.")

        # Final summary
        print("\n" + "="*80)
        print("📈 FINAL SUMMARY")
        print("="*80)
        print(f"  • Period: {report['period_name']}")
        print(f"  • Total repositories: {report['total_repos']}")
        print(f"  • Active repositories: {report['summary']['active_repos']}")
        print(f"  • Total commits: {report['summary']['total_commits']}")
        print(f"  • Total pull requests: {report['summary']['total_prs']}")
        print(f"  • New repositories: {len(report['new_repos'])}")
        print(f"  • Unique contributors: {len(report['summary']['unique_contributors'])}")

        if self.mode in ['weekly', 'monthly'] and report['summary']['total_commits'] > 0:
            avg_commits_per_day = report['summary']['total_commits'] / report['period_days']
            print(f"  • Average commits/day: {avg_commits_per_day:.1f}")

        print("="*80)
        print(f"\n📄 Report generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC")


def export_to_markdown(report: Dict, filename: str = None) -> str:
    """Export report to Markdown format for easy sharing"""
    if not filename:
        mode_prefix = report['mode']
        date_str = datetime.now().strftime('%Y%m%d')
        filename = f"github_activity_{report['target']}_{mode_prefix}_{date_str}.md"

    with open(filename, 'w', encoding='utf-8') as f:
        # Header
        mode_title = report['mode'].upper()
        f.write(f"# {mode_title} GitHub Activity Report: {report['target']}\n\n")

        period_start = datetime.fromisoformat(report['since_date']).strftime('%Y-%m-%d %H:%M:%S')
        period_end = datetime.fromisoformat(report['until_date']).strftime('%Y-%m-%d %H:%M:%S')
        f.write(f"**Period:** {report['period_name']} ({period_start} to {period_end} UTC)\n\n")

        # Executive Summary
        f.write("## Executive Summary\n\n")
        f.write(f"- **Active repositories:** {report['summary']['active_repos']}/{report['total_repos']}\n")
        f.write(f"- **Total commits:** {report['summary']['total_commits']}\n")
        f.write(f"- **Total pull requests:** {report['summary']['total_prs']}\n")
        f.write(f"- **Unique contributors:** {len(report['summary']['unique_contributors'])}\n")
        f.write(f"- **New repositories:** {len(report['new_repos'])}\n")
        if report.get('issue_activity') is not None:
            f.write(f"- **Issues opened/closed:** {report['summary']['total_issues_opened']}/{report['summary']['total_issues_closed']}\n")
        if report.get('new_releases') is not None:
            f.write(f"- **New releases:** {len(report['new_releases'])}\n")
        f.write("\n")

        # Top Contributors
        if report['top_contributors']:
            f.write("## Top Contributors\n\n")
            f.write("| Contributor | Commits |\n")
            f.write("|------------|---------|\n")
            for contributor, count in sorted_contributors(report['top_contributors'], limit=10):
                f.write(f"| {contributor} | {count} |\n")
            f.write("\n")

        # PR Contributors (heads-up reference)
        if report.get('pr_contributors'):
            f.write("## PR Contributors\n\n")
            f.write("| Author | PRs |\n")
            f.write("|--------|-----|\n")
            for contributor, count in sorted_pr_contributors(report['pr_contributors']):
                f.write(f"| {contributor} | {count} |\n")
            f.write("\n")

        # Daily Breakdown
        if report['daily_breakdown']:
            f.write("## Daily Activity Breakdown\n\n")
            f.write("| Date | Commits | Pull Requests | Total |\n")
            f.write("|------|---------|---------------|-------|\n")
            sorted_days = sorted(report['daily_breakdown'].items())
            for day, counts in sorted_days:
                total = counts['commits'] + counts['prs']
                f.write(f"| {day} | {counts['commits']} | {counts['prs']} | {total} |\n")
            f.write("\n")

        # New Repositories
        if report['new_repos']:
            f.write("## New Repositories\n\n")
            for repo in report['new_repos']:
                created = date_parser.parse(repo['created_at']).strftime('%Y-%m-%d')
                f.write(f"- **[{repo['full_name']}]({repo['html_url']})** - Created {created}\n")
                if repo['description']:
                    f.write(f"  - {repo['description']}\n")
            f.write("\n")

        # New Releases
        if report.get('new_releases'):
            f.write("## New Releases\n\n")
            for release in report['new_releases']:
                published = date_parser.parse(release['published_at']).strftime('%Y-%m-%d')
                f.write(f"- **{release['repo']}** — [{release['name']}]({release['url']}) ({release['tag_name']}) - {published}\n")
            f.write("\n")

        # Issue Activity
        if report.get('issue_activity') is not None:
            f.write("## Issue Activity\n\n")
            f.write(f"- **Opened:** {report['summary']['total_issues_opened']}\n")
            f.write(f"- **Closed:** {report['summary']['total_issues_closed']}\n")
            f.write(f"- **Currently open (touched this period):** {report['summary']['currently_open_issues']}\n\n")
            if report['issue_activity']:
                f.write("| Repository | Opened | Closed |\n")
                f.write("|------------|--------|--------|\n")
                for repo_name, issue_summary in report['issue_activity'].items():
                    f.write(f"| {repo_name} | {issue_summary['opened_count']} | {issue_summary['closed_count']} |\n")
                f.write("\n")

        # PR Quality Metrics
        if report.get('pr_metrics'):
            f.write("## PR Quality Metrics\n\n")
            metrics_summary = pr_metrics_summary(report['pr_metrics'])
            if metrics_summary['avg_merge_hours'] is not None:
                f.write(f"- **Avg time to merge:** {metrics_summary['avg_merge_hours']:.1f}h\n")
            if metrics_summary['avg_review_hours'] is not None:
                f.write(f"- **Avg time to first review:** {metrics_summary['avg_review_hours']:.1f}h\n")
            f.write(f"- **Lines changed:** +{metrics_summary['total_additions']} / -{metrics_summary['total_deletions']}\n\n")

        # Stale PRs
        if report.get('stale_prs'):
            f.write(f"## Stale Pull Requests (idle {report.get('stale_days', '?')}+ days)\n\n")
            for pr in sorted(report['stale_prs'], key=lambda x: -x['days_idle']):
                f.write(f"- **{pr['repo']}** — [#{pr['number']}]({pr['url']}) {pr['title']} ({pr['author']}, idle {pr['days_idle']}d)\n")
            f.write("\n")

        # First-time Contributors
        if report.get('first_time_contributors'):
            f.write("## First-Time Contributors\n\n")
            for name in report['first_time_contributors']:
                f.write(f"- {name}\n")
            f.write("\n")

        # Activity by Repository
        if report['repo_activity']:
            f.write("## Repository Activity\n\n")

            for repo_name, activity in sorted_repo_activity(report['repo_activity'])[:20]:
                f.write(f"### [{repo_name}]({activity['repo_url']})\n\n")
                f.write(f"- **Commits:** {activity['commit_count']}\n")
                f.write(f"- **Pull Requests:** {activity['pr_count']}\n")

                if activity['description']:
                    f.write(f"- **Description:** {activity['description']}\n")

                if activity['commits']:
                    f.write("\n**Recent Commits:**\n")
                    for commit in activity['commits'][:5]:
                        commit_date = datetime.fromisoformat(commit['date']).strftime('%Y-%m-%d %H:%M')
                        f.write(f"- [`{commit['sha']}`]({commit['url']}) - {commit['message']} ({commit['author']} @ {commit_date})\n")

                if activity['pull_requests']:
                    f.write("\n**Pull Requests:**\n")
                    for pr in activity['pull_requests'][:5]:
                        f.write(f"- [#{pr['number']}]({pr['url']}) - {pr['title']} ({pr['author']}, {pr['state']})\n")

                f.write("\n---\n\n")

        # Footer
        f.write(f"\n*Report generated on {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC*\n")

    print(f"\n📄 Markdown report exported to: {filename}")
    return filename


def parse_comma_list(value: Optional[str]) -> List[str]:
    """Split a comma-separated CLI value into stripped, non-empty items.

    Returns an empty list for None or an empty/whitespace-only string, which
    allows e.g. --email-bcc "" to be used to clear a saved BCC list.
    """
    if not value:
        return []
    return [item.strip() for item in value.split(',') if item.strip()]


def manage_targets(args: argparse.Namespace) -> bool:
    """Handle target (org/user)/email management commands.

    Returns True if a management command was handled (caller should stop),
    False if the caller should proceed to run the main checker.
    """
    if args.add_target:
        ConfigManager.add_target(args.add_target, args.set_default)
    elif args.remove_target:
        ConfigManager.remove_target(args.remove_target)
    elif args.list_targets:
        targets = ConfigManager.list_targets()
        default = ConfigManager.get_default_target()

        if not targets:
            print("📭 No targets configured. Add one with: --add-target TARGET")
        else:
            print("\n📋 Configured Targets:")
            for target in targets:
                default_marker = " (default)" if target == default else ""
                print(f"  • {target}{default_marker}")
            print()
    elif args.set_default:
        ConfigManager.set_default_target(args.set_default)
    elif args.configure_email:
        if not args.email_to:
            print("❌ --email-to is required when using --configure-email.")
            print("   Example: --configure-email you@example.com --email-to admin@example.com")
            return True
        ConfigManager.configure_email(
            args.configure_email,
            parse_comma_list(args.email_to),
            args.email_prefix,
            parse_comma_list(args.email_bcc)
        )
    elif args.email_bcc is not None:
        # --email-bcc given on its own (no --configure-email): update just
        # the saved BCC list on the existing email config.
        bcc_addrs = parse_comma_list(args.email_bcc)
        if ConfigManager.update_email_bcc(bcc_addrs):
            if bcc_addrs:
                print(f"✅ BCC list updated: {', '.join(bcc_addrs)}")
            else:
                print("✅ BCC list cleared.")
        else:
            print("❌ No email configuration found yet. Run --configure-email first, e.g.:")
            print("   --configure-email you@example.com --email-to admin@example.com --email-bcc audit@example.com")
    else:
        # No management commands, run the main checker
        return False

    return True


def build_arg_parser() -> argparse.ArgumentParser:
    """Construct the CLI argument parser."""
    parser = argparse.ArgumentParser(
        description='Check GitHub organization or user activity with daily/weekly/monthly reports',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Target Management (a target is an org or a personal username):
  -a, --add-target TARGET      Add a target to config
  -r, --remove-target TARGET   Remove a target from config
  -l, --list-targets           List all configured targets
  -s, --set-default TARGET     Set default target
  -n, --no-default             Ignore default target (require explicit target)

Email Configuration (requires msmtp):
  -c, --configure-email EMAIL  Configure email sender (e.g., user@example.com)
  -e, --email-to LIST          Comma-separated list of recipients
  -E, --email-prefix PREFIX    Email subject prefix (default: [GitHub Activity])
  -B, --email-bcc LIST         Comma-separated BCC list, saved to config (used
                              only when -b/--bcc is also passed on a send).
                              Can be set alone, without --configure-email, to
                              update just the BCC list on an existing config.
  -S, --send-email             Send report via email after generation
  -b, --bcc                    Include the saved BCC addresses on this send

Optional extras (each adds extra API calls, use only what you need):
  -i, --include-issues    Track issues opened/closed/still-open in the period
  -I, --include-releases  Track new releases/tags published in the period
  -p, --pr-metrics         Time-to-merge, time-to-first-review, lines changed
                          (adds 2 extra API calls per PR in range)
  -D, --stale-days DAYS    Flag open PRs idle for DAYS+ (1 extra call per repo)
  -N, --track-new-contributors
                          Flag first-time contributors using a local history
                          file under ~/.github-activity-checker/contributors/

Examples:
  # Configure email
  python github_activity_checker.py -c reports@example.com -e admin@example.com,team@example.com -B audit@example.com

  # Run and send email
  python github_activity_checker.py --weekly --send-email

  # Run and send email, including the saved BCC list
  python github_activity_checker.py -w -S -b

  # Weekly report with issues, releases, and stale-PR detection
  python github_activity_checker.py -w -i -I -D 14
        """
    )

    # Target management arguments
    parser.add_argument('-a', '--add-target', metavar='TARGET', help='Add a target (org or user) to config')
    parser.add_argument('-r', '--remove-target', metavar='TARGET', help='Remove a target from config')
    parser.add_argument('-l', '--list-targets', action='store_true', help='List configured targets')
    parser.add_argument('-s', '--set-default', metavar='TARGET', help='Set default target')
    parser.add_argument('-n', '--no-default', action='store_true', help='Ignore default target')

    # Email arguments
    parser.add_argument('-c', '--configure-email', metavar='EMAIL', help='Configure email sender address')
    parser.add_argument('-e', '--email-to', metavar='LIST', help='Comma-separated recipient list')
    parser.add_argument('-E', '--email-prefix', metavar='PREFIX', default='[GitHub Activity]', help='Email subject prefix')
    parser.add_argument('-B', '--email-bcc', metavar='LIST',
                         help='Comma-separated BCC list. With --configure-email, saves it alongside '
                              'from/to. Used alone (no --configure-email), it updates just the BCC '
                              'list on an existing config. Pass an empty string to clear it. Not '
                              'sent unless -b/--bcc is also passed on a send.')
    parser.add_argument('-S', '--send-email', action='store_true', help='Send report via email')
    parser.add_argument('-b', '--bcc', action='store_true',
                         help='Include the saved BCC addresses (from --configure-email --email-bcc) on this send')

    # Target argument (optional if default exists)
    parser.add_argument('target', nargs='?', help='GitHub organization or username (optional if default configured)')

    # Mode selection
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument('-d', '--daily', action='store_true', help="Daily mode - today's activity only")
    mode_group.add_argument('-w', '--weekly', action='store_true', help='Weekly mode - last 7 days')
    mode_group.add_argument('-m', '--monthly', action='store_true', help='Monthly mode - last 30 days')
    mode_group.add_argument('-C', '--custom', type=int, metavar='DAYS', help='Custom mode - last N days')

    parser.add_argument('-t', '--token', help='GitHub personal access token')
    parser.add_argument('-x', '--export-md', action='store_true', help='Export report to Markdown file')
    parser.add_argument('-o', '--output', help='Output filename for Markdown (default: auto-generated)')

    # Optional extras
    parser.add_argument('-i', '--include-issues', action='store_true', help='Track issue activity (opened/closed/still-open)')
    parser.add_argument('-I', '--include-releases', action='store_true', help='Track new releases/tags')
    parser.add_argument('-p', '--pr-metrics', action='store_true',
                         help='Include PR quality metrics: time-to-merge, time-to-first-review, lines changed '
                              '(adds 2 extra API calls per PR)')
    parser.add_argument('-D', '--stale-days', type=int, default=None, metavar='DAYS',
                         help='Flag open PRs with no activity for DAYS+ (adds 1 extra API call per repo)')
    parser.add_argument('-N', '--track-new-contributors', action='store_true',
                         help='Flag first-time contributors using a locally persisted history file')

    return parser


def resolve_target(args: argparse.Namespace, parser: argparse.ArgumentParser) -> str:
    """Determine which target (org or user) to run against, using the default if applicable."""
    target = args.target

    if not target and not args.no_default:
        target = ConfigManager.get_default_target()
        if target:
            print(f"📌 Using default target: {target}\n")

    if not target:
        parser.error("Target (organization or username) required. Either:\n"
                     "  • Pass as argument: script.py TARGET\n"
                     "  • Set a default target: --add-target TARGET --set-default\n"
                     "  • Use --no-default to bypass default target check")

    return target


def resolve_mode(args: argparse.Namespace) -> (str, Optional[int]):
    """Determine the reporting mode and custom day count from parsed args."""
    if args.daily:
        return 'daily', None
    if args.weekly:
        return 'weekly', None
    if args.monthly:
        return 'monthly', None
    if args.custom is not None:
        return 'custom', args.custom
    return 'weekly', None  # Default mode


def build_email_text_body(report: Dict, mode: str) -> str:
    """Build the plain-text email body for a report."""
    text_buffer = [
        f"{mode.upper()} Activity Report: {report['target']}\n",
        f"Period: {report['period_name']}",
        f"From: {datetime.fromisoformat(report['since_date']).strftime('%Y-%m-%d %H:%M:%S')} UTC",
        f"To: {datetime.fromisoformat(report['until_date']).strftime('%Y-%m-%d %H:%M:%S')} UTC",
        "\n" + "="*60,
        f"Active Repos: {report['summary']['active_repos']}/{report['total_repos']}",
        f"Total Commits: {report['summary']['total_commits']}",
        f"Total PRs: {report['summary']['total_prs']}",
        f"Contributors: {len(report['summary']['unique_contributors'])}",
        f"New Repos: {len(report['new_repos'])}",
        "="*60,
    ]

    if report['top_contributors']:
        text_buffer.append("\nTop Contributors:")
        for contributor, count in sorted_contributors(report['top_contributors'], limit=5):
            text_buffer.append(f"  {contributor}: {count} commits")

    if report.get('pr_contributors'):
        text_buffer.append("\nPR Contributors:")
        for contributor, count in sorted_pr_contributors(report['pr_contributors']):
            text_buffer.append(f"  {contributor}: {count} PRs")

    return "\n".join(text_buffer)


def main():
    """Entry point: parse args, run the checker, and handle export/email."""
    parser = build_arg_parser()
    args = parser.parse_args()

    # Handle management commands first
    if manage_targets(args):
        return

    target = resolve_target(args, parser)
    mode, custom_days = resolve_mode(args)

    # Validate token
    if not args.token and not os.getenv('GITHUB_TOKEN'):
        print("⚠️  Warning: No GitHub token provided. Rate limits will be very low (60 requests/hour).")
        print("   Get a token at: https://github.com/settings/tokens")
        print("   Then set environment variable: export GITHUB_TOKEN=your_token\n")
        if mode in ['weekly', 'monthly']:
            print("   Note: Weekly/monthly reports may hit rate limits without a token!\n")
        if args.pr_metrics:
            print("   Note: --pr-metrics adds 2 extra API calls per PR and will hit rate limits fast without a token!\n")

    # Create checker and run
    try:
        checker = GitHubActivityChecker(
            target=target,
            token=args.token,
            mode=mode,
            custom_days=custom_days,
            include_issues=args.include_issues,
            include_releases=args.include_releases,
            pr_metrics=args.pr_metrics,
            stale_days=args.stale_days,
            track_new_contributors=args.track_new_contributors,
        )

        report = checker.check_activity()
        checker.print_report(report)

        # Export to markdown if requested
        if args.export_md:
            export_to_markdown(report, args.output)

        # Send email if requested
        if args.send_email:
            email_config = ConfigManager.load_email_config()
            if not email_config:
                print("\n❌ Email not configured. Run: --configure-email EMAIL --email-to RECIPIENTS")
            else:
                text_body = build_email_text_body(report, mode)
                html_body = EmailSender.generate_html_report(report)
                subject = f"{report['target']} - {mode.title()} Activity Report"
                EmailSender.send_email(subject, text_body, html_body, email_config, use_bcc=args.bcc)

    except KeyboardInterrupt:
        print("\n\n⚠️  Script interrupted by user")
        sys.exit(0)
    except Exception as e:
        print(f"\n❌ Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
