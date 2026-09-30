# seo-tune

A technical and brand SEO skill for **Claude Code and Codex**. Audit a site's
served HTML and crawl configuration, use real search-query data to prioritize
fixes, and compare the result with a saved baseline.

Includes a portable agent workflow, dependency-free audit scripts, synthetic
HTTP fixtures, 36 regression tests, and an optional IndexNow submission template.

## What it checks

- Canonical and alternate hosts, redirects, soft 404s, crawler policies, and
  staging/preview exposure.
- Titles, descriptions, canonical tags, robots directives, H1 counts, JSON-LD,
  share images, sitemap entries, and metadata placement.
- Googlebot, Bingbot, and browser **user-agent response differences**. These are
  HTTP requests with those headers, not requests from verified crawler IPs.
- Critical drift between scans, including new noindex, failed pages, changed
  canonicals, lost schema types, and missing share images.

The skill treats length and heading checks as review heuristics, not ranking
scores. Failed or blocked measurements are reported explicitly. Audit scripts
are read-only; site changes, deployments, and IndexNow submissions follow the
user's authorization and the target repository's rules.

## Install

Prerequisites: Python 3, Bash, curl, and Git. The optional IndexNow template
requires Node.js 18+ and configuration for your own domain and public key file.
No third-party runtime packages, API keys, or other skills are required.

### Codex

```sh
mkdir -p ~/.agents/skills
git clone https://github.com/forrestelliott/seo-tune.git ~/.agents/skills/seo-tune
```

Open your site's repository in Codex and invoke `$seo-tune`. The installation
path follows [Codex's local skill discovery documentation](https://learn.chatgpt.com/docs/build-skills#where-codex-loads-local-skills).

### Claude Code

```sh
mkdir -p ~/.claude/skills
git clone https://github.com/forrestelliott/seo-tune.git ~/.claude/skills/seo-tune
```

Open your site's repository in Claude Code and invoke `/seo-tune`. This uses
[Claude Code's personal skills directory](https://code.claude.com/docs/en/skills#where-skills-live).

If you use both, keep one checkout and symlink it into the other agent's skill
folder. Existing `seo-tune` directories must be reviewed before replacing them.

## Run the scripts directly

Run from the skill checkout, replacing `example.com` with a site you control:

```sh
bash scripts/hosts.sh example.com alternate.example.com
python3 scripts/sweep.py https://example.com --save /tmp/seo-before.json
python3 scripts/sweep.py https://example.com --compare /tmp/seo-before.json --save /tmp/seo-after.json
python3 scripts/sweep.py /path/to/project/dist
```

The sweep defaults to all three user agents. `--quick` checks only Googlebot;
`--all` prints every page instead of just findings.

| Sweep exit code | Meaning |
| --- | --- |
| `0` | Scan completed without critical comparison drift; findings may still exist. |
| `1` | Critical drift against the supplied baseline. |
| `2` | Incomplete measurement or invalid input; do not claim a verified scan. |

Snapshots record response coverage and effective meta/HTTP-header noindex.
Use distinct before/after files. Built-folder snapshots identify relative files;
live snapshots identify URLs, so do not compare the two without mapping them.
A built-folder scan also includes utility pages and SPA shells; review those
separately from sitemap-indexable pages. The scripts inspect HTML before
JavaScript executes, and they do not replace browser or rendered-image checks.

`hosts.sh` prints human-reviewed diagnostics; its exit code is not a pass/fail
SEO gate. Live audits make network requests to sitemap URLs, canonical targets,
share images, and the host variants/subdomains shown in the script. Review the
scope before running on an unfamiliar site.

## Test

```sh
python3 -m unittest discover -s tests -v
bash -n scripts/hosts.sh
node --check templates/indexnow.mjs
```

Tests use synthetic pages and an ephemeral loopback HTTP server. They require
no external account, live website, or internet access, though sandboxed agents
may need permission to open the local listener. The suite checks clean controls,
seeded faults, incomplete measurements, and critical-drift exit codes.

For manual inspection:

```sh
python3 tests/fixture_server.py --port 0
```

The printed URL serves `/clean.xml` (clean control), `/sitemap.xml` (intentional
faults), `/bad-index.xml` (failed child sitemap), and `/ua-failure.xml` (one
user-agent response unavailable).

## IndexNow

`templates/indexnow.mjs` is an opt-in template, not part of the audit. Copy it
into the target project's scripts directory, configure `HOST` and `PUBLIC_DIR`,
and create that site's own verification key. Use `--dry-run` first. A real run
submits URLs to IndexNow only after confirming the public key file is live.
Submission does not guarantee crawling, indexing, or ranking, and Google does
not use IndexNow.

## Privacy

This repository contains generic instructions, scripts, and synthetic tests.
It does not include client sites, customer data, private analytics, audit
exports, credentials, deployment identifiers, or local machine paths.
Keep your own reports and snapshots in the target project's appropriate private
location. Review generated artifacts before publishing them.

## License

[MIT](LICENSE).
