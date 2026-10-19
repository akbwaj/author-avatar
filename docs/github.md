# Uploading to GitHub

The repository includes source, offline tests, placeholder configuration, the
book source fixture with its original notices, and an ingestion inspection
report. `.gitignore` excludes local environments, credentials, SQLite databases
and their sidecars/backups, logs, and local tool settings.

## Review and test

From the project root:

```sh
source .venv/bin/activate
python -m pip install -e '.[test]'
python -m pytest -o addopts='' -q
git status --short
git diff --cached --stat
git diff --cached --check
```

Never stage `.env`, `.env.demo`, credentials, a conversation database, its
journals, or database backups. Review generated reports for local paths or
private content before sharing them; the included report uses a relative path.

The application code has no selected redistribution license yet. Choose a code
license before advertising the project as open source. The bundled book retains
its own notices and license, described in [data/README.md](../data/README.md).

## First upload

Create an **empty** repository named `author-avatar` on GitHub, choosing the
visibility you intend. Do not initialize a second README, license or gitignore.
Replace `YOUR_USERNAME` below with the repository owner's actual account name:

```sh
git add .
git diff --cached --stat
git diff --cached --check
git commit -m "Initial Author Avatar implementation"
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/author-avatar.git
git push -u origin main
```

These remote commands assume no existing `origin`; inspect `git remote -v`
first. Use GitHub's supported Git authentication rather than embedding a token
in the remote URL. No remote is created or published by the preparation step.

Git requires author and committer timestamps. Use the current time: it records
when you commit the code, not when development began or how long it took.
Placeholder dates cause GitHub to display misleading commit ages. GitHub also
records upload and other platform activity times.

## Correcting the initial placeholder timestamp

If you followed an earlier version of this guide and your initial commit shows
an old placeholder date, update both timestamps to the current time. The guide
correction can be included in the amended commit:

```sh
previous_commit=$(git rev-parse HEAD)
git add docs/github.md
env -u GIT_AUTHOR_DATE -u GIT_COMMITTER_DATE git commit --amend --no-edit --date=now
git push --force-with-lease="refs/heads/main:$previous_commit" origin main
```

This replaces the already-published initial commit with a new commit hash. Use
it only for your initial branch when collaborators have not based work on it.
This explicit `--force-with-lease` refuses the push unless the remote branch
still points at the original commit you captured. Do not replace it with `--force`
if the push is rejected. See the official
[commit](https://git-scm.com/docs/git-commit) and
[push](https://git-scm.com/docs/git-push) documentation.

## Continuous integration

[Offline tests](../.github/workflows/tests.yml) run on pushes, pull requests and
manual dispatch using Python 3.10 and 3.12. Dependencies are installed first;
the tests themselves use fake providers and local fixtures, with no Ollama,
AWS account, model download or GitHub secrets needed. Standalone live smoke
scripts are excluded from pytest collection. Workflow actions are pinned to
release commit hashes, and repository permissions are read-only.

The workflow follows GitHub's
[Python build-and-test documentation](https://docs.github.com/en/actions/tutorials/build-and-test-code/python).
Its first actual GitHub run happens after upload; local pytest success is not
a claim that GitHub-hosted CI has already run.
