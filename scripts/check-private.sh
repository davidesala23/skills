#!/usr/bin/env bash
# Block publication of private data. Fails closed: no terms file, no push.
#
# Usage: scripts/check-private.sh [<rev-range>]
#   no argument  -> scan the working tree (tracked and untracked, not ignored)
#   <rev-range>  -> scan every commit in the range: file contents, messages,
#                   author and committer identities
#
# The term list is private by definition, so it never lives in this repo.
# Point git at it once per clone:
#   git config skills.privateTerms /absolute/path/private-terms.txt
# One term per line, matched case-insensitively as a fixed string on word boundaries
# (so a term inside a longer word, such as a username, does not match).
# Lines starting with # and blank lines are ignored.
set -euo pipefail

terms_file="$(git config --get skills.privateTerms || true)"
if [[ -z "$terms_file" || ! -s "$terms_file" ]]; then
  echo "check-private: no term list. Set 'git config skills.privateTerms <file>'. Refusing." >&2
  exit 2
fi

patterns="$(mktemp)"
trap 'rm -f "$patterns"' EXIT
grep -v -e '^[[:space:]]*#' -e '^[[:space:]]*$' "$terms_file" > "$patterns" || true
if [[ ! -s "$patterns" ]]; then
  echo "check-private: term list $terms_file is empty. Refusing." >&2
  exit 2
fi

# LICENSE carries the copyright holder by design.
exclude=(':(exclude)LICENSE')
found=0

if [[ $# -eq 0 ]]; then
  if git grep -n -I -i -w -F -f "$patterns" --untracked -- . "${exclude[@]}"; then found=1; fi
else
  for rev in $(git rev-list "$1"); do
    if git grep -n -I -i -w -F -f "$patterns" "$rev" -- . "${exclude[@]}"; then found=1; fi
    if git log -1 --format='%an <%ae>%n%cn <%ce>%n%B' "$rev" | grep -i -w -F -f "$patterns"; then
      echo "  ^ in metadata of commit $rev" >&2
      found=1
    fi
  done
fi

if [[ $found -ne 0 ]]; then
  echo "check-private: private terms found above. Remove them before publishing." >&2
  exit 1
fi
echo "check-private: clean."
