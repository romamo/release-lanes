# S-007: Guide installing the gate's App on more repos

status: built

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

With the App's JWT: `GET /app` for the slug and owner, `GET /app/installations` for each
installation's account and `repository_selection`, and `GET /repos/<owner>/<repo>/installation`
for whether a repo is covered (200 yes, 404 no, anything else exits 2 naming the repo).

### What it guides

shipmill never installs the App or adds a repo for the maintainer: they may have many
accounts and repos, and which ones get the App is theirs to choose. For the repos the App
doesn't cover, it opens one page, the App's **Install App** page, which lists every account
with an **Install** or **Configure** button:

- `https://github.com/settings/apps/<slug>/installations` for an App the login owns
- `https://github.com/organizations/<org>/settings/apps/<slug>/installations` for an org's

and says, per account of a missing repo, what to do there:

```
shipmill-romamo doesn't cover cli-agent-spec/cli-agent-spec yet; GitHub needs your click. Open https://github.com/settings/apps/shipmill-romamo/installations
  cli-agent-spec: click Install, choose Only select repositories, pick cli-agent-spec/cli-agent-spec
```

An account with no installation reads `click Install, choose Only select repositories, pick
<repos>`; one with an installation on selected repositories reads `click Configure, add
<repos> under Repository access, Save`. A repo already covered prints `already installed on
<owner/repo>`, and with every repo covered nothing opens.

It opens the page in the browser (`webbrowser.open`; `--no-browser` only prints), then
checks every 5 seconds, for up to 10 minutes, each repo not yet covered, printing `installed
on <owner/repo>` as each appears. It exits 0 when every repo is covered, and 1 naming the
repos still missing when the time runs out. Every line is flushed as printed (S-006-19).

`--json` prints one object: `app_id`, `slug`, `page`, and `repos`, a list of `{repo,
account, action, installed}`, where `action` is `none`, `install`, or `add`.

### app-create

`shipmill app-create`'s install step (spec 006, Install) uses the same guide for the repos it
planned, so it opens the App's Install App page and says per account what to pick.

### Docs

- `docs/install.md`, Give the sessions their own identity: `shipmill app-install` for a repo
  connected later or in another account
- `skills/shipmill-setup/SKILL.md`, The gate: with `app_id` set, run `$CR app-install
  <owner/repo>` for the repo being connected, and relay its steps to the user

## Acceptance criteria

- S-007-1: with no repo named, the command takes the checkout's `origin` repo, and exits 2 saying to name one outside a GitHub checkout
- S-007-2: `--app-id` defaults to `[agents] app_id`; with neither the command exits 2 saying to pass `--app-id`; the key is checked as spec 004 checks it
- S-007-3: a repo `GET /repos/<repo>/installation` answers 200 for prints `already installed on <repo>` and opens no page; any status but 200 or 404 exits 2 naming the repo
- S-007-4: with repos not covered, the command prints that GitHub needs a click and the App's Install App page (the user or the org form, by the App's owner), and opens that page once; it installs and adds nothing itself
- S-007-5: per account of a missing repo it prints `click Install, choose Only select repositories, pick <repos>` when the account has no installation, and `click Configure, add <repos> under Repository access, Save` when it has one
- S-007-6: after guiding, the command checks every 5 seconds for up to 10 minutes, prints `installed on <repo>` as each appears, exits 0 when all are covered, and exits 1 naming each repo still missing
- S-007-7: `--no-browser` opens nothing and prints the same page; every line is flushed as printed
- S-007-8: `--json` prints one object with `app_id`, `slug`, `page`, and each repo's `repo`, `account`, `action`, and `installed`
- S-007-9: `shipmill app-create`'s install step guides the planned repos the same way
- S-007-10: `docs/install.md` and `skills/shipmill-setup/SKILL.md` document `shipmill app-install` and that adding a repo takes a click on GitHub

## Out of scope

- Installing or adding repos for the maintainer: which accounts and repos get the App is theirs to choose, and GitHub's API refuses the `gh` login's token for it anyway
- Removing a repo from an installation, or uninstalling
- Setting `app_id` in the repo's config: a reviewed commit (D-4), as in spec 006

## Decisions relied on

- D-4
- D-14

## Issues

- shipmill/shipmill#172: S-007-1, S-007-2, S-007-3, S-007-4, S-007-5, S-007-6, S-007-7, S-007-8, S-007-9, S-007-10

## Verification

Checked on main plus #172's PR, with a fake of GitHub's App API in the tests; the App's own
API (`GET /app`, `GET /app/installations`, `GET /repos/<repo>/installation`) was also read
for real with the `shipmill-romamo` App's JWT on 2026-10-06, which showed one installation
(shipmill, all repositories) and cli-agent-spec/cli-agent-spec not covered.

- S-007-1: `test_s007_1_the_default_repo_is_origins`, passing
- S-007-2: `test_s007_2_the_app_id_comes_from_the_config_or_the_flag`, passing
- S-007-3: `test_s007_3_a_covered_repo_opens_nothing` and `test_s007_3_another_status_names_the_repo`, passing
- S-007-4: `test_s007_4_missing_repos_open_the_install_app_page_once` and `test_s007_4_an_org_owned_app_opens_the_orgs_page`, passing; the fake raises on any POST, so nothing is installed or added
- S-007-5: `test_s007_5_each_account_is_told_to_install_or_configure`, passing
- S-007-6: `test_s007_6_installations_are_reported_and_the_wait_ends` and `test_s007_6_and_7_a_missing_repo_exits_1_with_lines_flushed`, passing
- S-007-7: `test_s007_7_no_browser_prints_the_page_and_opens_nothing` and `test_s007_6_and_7_a_missing_repo_exits_1_with_lines_flushed`, passing
- S-007-8: `test_s007_8_json_lists_each_repo`, passing
- S-007-9: `test_s007_9_app_create_guides_its_install_step_the_same_way`, passing; app-create's tests run its install step through the guide
- S-007-10: `test_s007_10_the_docs_document_app_install`, passing
