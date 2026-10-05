# shipmill

Issue to release on GitHub: agent skills in `skills/` and the `shipmill` CLI and reusable
workflows that cut releases on lanes by policy. Public, MIT. shipmill releases itself with
its own workflows.

## Layout

- `src/shipmill/`: the package and CLI (`shipmill.cli:run`)
- `skills/<name>/`: `SKILL.md` plus `scripts/`, `agents/`, `references/`; shipped in the
  wheel because the gate runs the skills' state scripts
- `.github/workflows/`: CI and the reusable release and operate workflows
- `.claude-plugin/`: the `shipmill@shipmill` plugin and marketplace
- `docs/decisions.md`, `docs/specs/`, `docs/postmortems/`, `docs/flow.md`

## Checks

Run what CI runs before calling a change done:

```bash
uv sync --locked
uv run ruff check src tests skills
uv run ruff format --check src tests skills
uv run mypy
uv run pytest -q
uv run --no-project python skills/github-issue-triage/scripts/specs.py check
uv run --no-project python skills/github-issue-triage/scripts/specs.py coverage
uvx --from actionlint-py actionlint .github/workflows/*.yml
```

- Package code needs Python 3.14 and strict mypy
- Skill scripts run with `uv run --no-project python` and must stay Python 3.10 compatible
  with no dependencies

## Rules

- Every pull request adds its own entry under `## [Unreleased]` in `CHANGELOG.md` (Keep a
  Changelog); never write a version section by hand, the release workflow stamps it
- Check a change against every entry in `docs/decisions.md` whose "Applies to" it touches.
  Change a rule by adding a new `D-N` that supersedes it, never by editing an old one
- A feature starts as a spec `docs/specs/NNN-<slug>.md` from `TEMPLATE.md`, merged before
  its build; a test that proves a criterion names its id (`S-NNN-1`)
- Prose calls the release automation shipmill, not "release bot" (D-3)
- Settings live in one file, `.github/shipmill.toml` (D-4)
- Work on a branch and land through a pull request; never push to `main`
- This repo is public: never reference private repos, customers, or paid features
