# skills

Agent skills for personal operations: ingesting the previous day into a knowledge base,
a daily trust check, a weekly review, and a reading pipeline. Each skill is a self-contained
folder under `skills/` with its own `SKILL.md`, references and scripts.

No skill is published yet. Skills land here once they are free of personal data and read
every user-specific value from configuration.

## Install

```sh
npx skills add davidesala23/skills
```

Or clone the repo and symlink a skill folder into your agent's skills directory
(for Claude Code: `.claude/skills/<name>`).

## Configuration

Skills contain no personal paths, names, schedules or routing rules. Each skill reads them
from `$SKILLS_CONFIG_DIR/<skill-name>/config.json`, and stops with an explicit message when
that file is missing instead of guessing. Every skill ships a `config.example.json` that
documents each key.

```sh
export SKILLS_CONFIG_DIR="$HOME/path/to/your/skills-config"
```

Keep that directory in your own private notes repository, so it is backed up without being
published.

## Contributing: privacy check

Every commit and push runs `scripts/check-private.sh` against a private term list kept
outside this repository. The check fails closed: without the list, nothing is pushed.

```sh
git config core.hooksPath .githooks
git config skills.privateTerms /absolute/path/private-terms.txt
scripts/check-private.sh            # scan the working tree
scripts/check-private.sh main       # scan every commit reachable from main
```

The hook protects only clones where it is configured. A push from any other machine must
configure the same two settings first.

## License

MIT
