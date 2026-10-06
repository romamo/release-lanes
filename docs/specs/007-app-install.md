# S-007: Guide installing the gate's App on more repos

status: draft

## Problem

`shipmill app-create` (spec 006) makes the App and waits for its first installations, but a
repo connected later, or one in another account, still needs the App, and the gate refuses to
launch for a repo it isn't installed on (S-004-5). Nothing tells the maintainer which page to
open or what to pick there. GitHub doesn't let a tool do it for them: there is no API to
install an App, and adding a repo to an existing installation (`PUT
/user/installations/<id>/repositories/<repo_id>`) refuses the `gh` login's token (403 on
2026-10-06); it takes a personal access token or a token the App issued to the user. So
shipmill guides the click and checks the result.

## Behaviour

### The command

```
shipmill app-install [owner/name ...] [--app-id <id>] [--app-key <path>] [--no-browser] [--json]
```

A new subcommand in `src/shipmill/cli.py`, built in a new module `src/shipmill/app_install.py`
on `src/shipmill/app.py`'s JWT and API. With no repo it takes the checkout's `origin` repo;
outside a GitHub checkout and with no repo it exits 2 saying to name one. `--app-id` defaults
to `[agents] app_id` in the checkout's `.github/shipmill.toml`; with neither it exits 2 saying
to pass `--app-id`. `--app-key` defaults to `~/.config/shipmill/app-<app_id>.pem` and is
checked as spec 004 checks it.

### What it reads

With the App's JWT: `GET /app` for the slug, `GET /app/installations` for each installation's
id, account login, account type, and `repository_selection`, and `GET
/repos/<owner>/<repo>/installation` for whether a repo is covered (200 yes, 404 no, anything
else exits 2 naming the repo). Anonymously, `GET /users/<account>` for an account's id.

### What it guides

Each named repo is covered already, or its account needs one of two pages. Repos are grouped
by account, so each account gets one page that lists every repo to pick there:

| The account | Page | What to do there |
|---|---|---|
| has no installation of the App | `https://github.com/apps/<slug>/installations/new/permissions?target_id=<account id>`: GitHub's install page with the account already chosen | choose **Only select repositories**, pick the listed repos, click **Install** |
| has one with selected repositories | the installation's settings: `https://github.com/organizations/<org>/settings/installations/<id>` for an org, `https://github.com/settings/installations/<id>` for the login | under **Repository access**, add the listed repos, click **Save** |

A repo whose account's installation covers all repositories is already covered and reads so.

The output, per account, before opening its page:

```
cli-agent-spec: shipmill-romamo isn't installed there yet (GitHub needs your click)
  open https://github.com/apps/shipmill-romamo/installations/new/permissions?target_id=274805548
  choose Only select repositories, pick: cli-agent-spec/cli-agent-spec; click Install
```

It opens each page in the browser (`webbrowser.open`; `--no-browser` only prints), then
checks every 5 seconds, for up to 10 minutes, each repo not yet covered, printing `installed
on <owner/repo>` as each appears. It exits 0 when every repo is covered, and 1 naming the
repos still missing when the time runs out. A repo covered from the start prints `already
installed on <owner/repo>` and opens nothing. Every line is flushed as printed (S-006-19).

`--json` prints one object: `app_id`, `slug`, and `repos`, a list of `{repo, account, action,
url, installed}`, where `action` is `none`, `install`, or `add` and `url` is the page or null.

### app-create

`shipmill app-create`'s install step (spec 006, Install) uses the same guide for the repos it
planned, so its output names each account's page and what to pick, instead of one generic
install link.

### Docs

- `docs/install.md`, Give the sessions their own identity: `shipmill app-install` for a repo
  connected later or in another account
- `skills/shipmill-setup/SKILL.md`, The gate: with `app_id` set, run `$CR app-install
  <owner/repo>` for the repo being connected, and relay its steps to the user

## Acceptance criteria

- S-007-1: with no repo named, the command takes the checkout's `origin` repo, and exits 2 saying to name one outside a GitHub checkout
- S-007-2: `--app-id` defaults to `[agents] app_id`; with neither the command exits 2 saying to pass `--app-id`; the key is checked as spec 004 checks it
- S-007-3: a repo `GET /repos/<repo>/installation` answers 200 for prints `already installed on <repo>` and opens no page; any status but 200 or 404 exits 2 naming the repo
- S-007-4: for an account with no installation, the command prints that GitHub needs a click, the install URL with `target_id` set to the account's id, and the repos to pick, and opens that URL once for all the account's repos
- S-007-5: for an account whose installation has selected repositories, the command prints and opens the installation's settings page (the org or the user form of the URL) and the repos to add
- S-007-6: after guiding, the command checks every 5 seconds for up to 10 minutes, prints `installed on <repo>` as each appears, exits 0 when all are covered, and exits 1 naming each repo still missing
- S-007-7: `--no-browser` opens nothing and prints the same URLs; every line is flushed as printed
- S-007-8: `--json` prints one object with `app_id`, `slug`, and each repo's `repo`, `account`, `action`, `url`, and `installed`
- S-007-9: `shipmill app-create`'s install step guides each planned account the same way
- S-007-10: `docs/install.md` and `skills/shipmill-setup/SKILL.md` document `shipmill app-install` and that adding a repo takes a click on GitHub

## Out of scope

- Installing or adding repos without a click: GitHub's API refuses the `gh` login's token for it; a personal access token or the App's user tokens could, as a later spec, at the cost of handling one more secret
- Removing a repo from an installation, or uninstalling
- Setting `app_id` in the repo's config: a reviewed commit (D-4), as in spec 006

## Decisions relied on

- D-4
- D-14

## Issues

## Verification
