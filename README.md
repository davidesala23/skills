# Agent skills

Reusable workflows with personal settings kept outside the published source.

## Available

| Skill | Use |
|---|---|
| [ingest](skills/ingest/SKILL.md) | Import meetings, captured readings and correspondence into an existing knowledge base |

Ingest saves useful knowledge automatically, routes unmatched subjects to a configured
area KB, and retains interrupted work. It does not create tasks or change capture
channels. Sources are enabled individually; a partial import reports its gaps.

## Install

Clone this repository, then link the skill into the host you use:

```sh
git clone https://github.com/davidesala23/skills.git
cd skills
mkdir -p "$HOME/.agents/skills"
ln -s "$PWD/skills/ingest" "$HOME/.agents/skills/ingest"
```

That example installs for Codex. For Claude Code, use `$HOME/.claude/skills/ingest`
as the destination instead. Keep one active installation per host. Hosts with a
different skill importer can import the `skills/ingest/` folder directly.

## Configure

Copy [config.example.json](skills/ingest/config.example.json) to
`$SKILLS_CONFIG_DIR/ingest/config.json` in a private directory, fill the values and
enable only configured sources. [CONFIG.md](skills/ingest/CONFIG.md) documents every key.
Missing or invalid configuration stops before provider access or writes.

```sh
export SKILLS_CONFIG_DIR="$HOME/path/to/private-skills-config"
python3 skills/ingest/scripts/ingest_config.py
```

Configuration output contains your account/path values; keep it private. Real configs,
capture queues, source recordings, credentials and raw correspondence never belong in
this repository. Back up private configuration in your own notes repository.

Core scripts use Python 3.10+ and its standard library. Provider access comes from the
host's authenticated tools and authorized browser. Personal IMAP uses an existing macOS
Keychain entry; local recording transcription uses a configured interpreter with
`mlx_whisper` and an already available local model. Neither is needed when that source
is disabled. KB saves use a configured [record writer](skills/ingest/references/records.md).

Invoke `$ingest` or `/ingest` in your host for a previous-day batch, an explicit date or
recovery of interrupted work. Installation does not create a schedule. Live provider
access and imported evidence need checking in the target environment.

## Verify

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover -s skills/ingest/tests -p 'test_*.py'
```

Node.js is used by the isolated Discord page-client tests. Fixtures do not contact
mail, browser sessions or transcription services. Tests and synthetic workflow proof
do not establish acceptance of a complete live import.

## Contributing: privacy check

Before committing or pushing, configure the existing hooks and a private deny-list
outside the repository:

```sh
git config core.hooksPath .githooks
git config skills.privateTerms /absolute/path/private-terms.txt
scripts/check-private.sh
scripts/check-private.sh main
```

The hook fails closed without the list and checks pushed commit contents and metadata.
It protects only configured clones; every publishing machine needs the same setup.
Do not commit configuration output or test artifacts containing personal data.

## License

MIT
