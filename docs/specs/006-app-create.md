# S-006: Create the gate's GitHub App in one click

status: built

## Problem

Spec 004 lets gated sessions write as a GitHub App, but leaves creating the App to the
maintainer: GitHub's settings form, nine permissions picked by hand, the webhook turned off,
a private key downloaded and moved to `~/.config/shipmill/app-<app_id>.pem` with mode
`0600`. Each of those is a step to get wrong, and a wrong permission only shows up when the
gate refuses a launch. The maintainer also has to decide who owns the App and whether it's
private or public, which decides where it can be installed: a private App only on the
account that owns it, a public one on any account. Someone whose gated repos sit in two
orgs, such as `shipmill/shipmill` and `cli-agent-spec/cli-agent-spec`, needs a public App
or one per org, and nothing tells them so. Spec 004 left the manifest flow out of scope
because it needs a browser; this spec adds it, so the App is made with one click.

## Behaviour

### The command

```
shipmill app-create [--owner <account>] [--public | --private] [--name <name>]
                    [--repos <owner/name,...>] [--dry-run] [--json] [--no-browser]
```

A new subcommand in `src/shipmill/cli.py`, built in a new module `src/shipmill/app_create.py`
next to `src/shipmill/app.py`. It reads GitHub through the host's `gh` login, as the gate's
own reads do (S-004-12), and needs the `read:org` scope; without it, it exits 2 saying
`gh auth refresh -s read:org`. It runs in four steps: discover, plan, create, install.

### 1. Discover

**Accounts.** The accounts that can own and install the App are the login itself (`GET
/user`) and every org where the login's membership is active with role `admin` (`GET
/user/memberships/orgs`). An org where the login is only a member is left out: GitHub lets
only an org's owners create an App under it or install one on it.

**Gated repos.** A gated repo is a repo in one of those accounts that has
`.github/shipmill.toml` on its default branch. For each account, the command lists its
repos (`GET /user/repos?affiliation=owner` for the login, `GET /orgs/<org>/repos` for an
org), skips archived ones, and asks `GET /repos/<owner>/<repo>/contents/.github/shipmill.toml`
for each: 200 is gated, 404 is not, anything else exits 2 naming the repo. Code search is
not used: it misses repos (it didn't find `shipmill/shipmill`'s config on 2026-10-06).

`--repos owner/name,...` skips the discovery of repos and takes the list as the gated repos;
each must be in an account the login administers, or the command exits 2 naming it.

### 2. Plan

The plan is the App's owner, its visibility, its name, and the gated repos it is for.
Unless the flags say otherwise:

| Gated repos found | Owner | Visibility |
|---|---|---|
| all in one account | that account | private |
| in several accounts | the account with the most of them; on a tie, the owner of the checkout's `origin` if it is among them, else the first by name | public |
| none | the owner of the checkout's `origin`, if the login administers it | private |

`--owner` sets the owner and `--public` or `--private` the visibility; the repos are still
discovered, since the install step checks them. With no gated repo found, no `--owner`, and a
checkout whose `origin` owner the login doesn't administer (or no checkout), the command
exits 2 saying to pass `--owner`. An `--owner` the login doesn't administer exits 2 naming
the account and the role it needs. `--private` with gated repos outside the owner account
prints a warning naming each repo the App can't be installed on, and goes on.

`--name` defaults to `shipmill-agent`. The plan is printed before anything is created:

```
plan: shipmill-agent under shipmill, public
  why: gated repos in 2 accounts (shipmill 1, cli-agent-spec 1)
  repos: cli-agent-spec/cli-agent-spec, shipmill/shipmill
```

`--dry-run` prints the plan and stops: it starts no server, opens no browser, and writes
nothing. `--json` prints the plan, and after a real run the result, as one JSON object:
`owner`, `owner_type` (`Organization` or `User`), `public`, `name`, `reason`, `repos`, and
after creation `app_id`, `slug`, `key`, and `installed` (the repos the App was found on).

### 3. Create

The command refuses to start when a key for the planned App can't be written: when
`~/.config/shipmill/` exists and is readable by group or others it exits 2 saying to
`chmod 700` it.

It serves one page on `127.0.0.1` at a free port and opens it in the browser
(`webbrowser.open`; with `--no-browser` it only prints the URL). The page posts the App's
manifest to `https://github.com/organizations/<owner>/settings/apps/new` for an org owner
or `https://github.com/settings/apps/new` for the login, with a random `state`. The
manifest:

- `name`, `public` from the plan; `url` the first gated repo's URL, or the owner's
- `default_permissions`: exactly `PERMISSIONS` from `src/shipmill/app.py`, so the App has
  what the gate checks and nothing more
- `hook_attributes.active: false` and no `default_events`: the App needs no webhook
- `redirect_url`: the local server's `/callback`

The maintainer clicks **Create GitHub App** on GitHub. GitHub redirects to `/callback` with
a `code`; a request with a missing or different `state` gets an error page and changes
nothing. The command posts the code to `POST /app-manifests/<code>/conversions` (GitHub
needs no token for it) and gets the App's `id`, `slug`, and `pem`. It writes the key to
`~/.config/shipmill/app-<id>.pem`, creating the directory with mode `0700` and the file
with mode `0600` in one `open` that fails if the file exists. The conversion's
`client_secret`, `webhook_secret`, and `client_id` are dropped: never printed, never
written. The key never appears in output.

If GitHub refused the name (it is taken), the maintainer changes it on GitHub's page; the
command uses the `slug` the conversion returns. The server stops after the one callback.
With no callback after 10 minutes, the command exits 2 with `no App was created`.

### 4. Install

The App exists, but on no repo. The command prints and opens
`https://github.com/apps/<slug>/installations/new` and the repos to select, then checks
every 5 seconds, for up to 10 minutes, `GET /repos/<owner>/<repo>/installation` with the
App's JWT (as `src/shipmill/app.py` signs it) for each gated repo in an account the App can
be installed on. It prints `installed on <owner/repo>` as each appears, and stops when all
have. When the time runs out it prints each repo still missing and exits 1: the App and key
stay, and the same URL installs it later.

It ends by printing what the repos' configs need, without editing any (D-4: a config change
is a reviewed commit):

```
created shipmill-agent (App ID 123456), key in ~/.config/shipmill/app-123456.pem
add to [agents] in each repo's .github/shipmill.toml:
  app_id = 123456
then prove it: shipmill --repo <gate checkout> gate <owner/repo> --dry-run
```

### Docs

- `docs/install.md`, Give the sessions their own identity: `shipmill app-create` replaces
  the hand-made App as the first way, with the plan's rules; the permission table and the
  manual steps stay as the fallback
- `skills/shipmill-setup/SKILL.md`, The gate: run `shipmill app-create --dry-run`, show the
  user the plan, then run it without `--dry-run`; the user clicks Create and selects the
  repos on GitHub
- `README.md`: one line on the command, if it lists the CLI's commands

## Acceptance criteria

- S-006-1: the accounts are the login and each org where its membership is active with role `admin`; an org where it is a member only is left out, and a token without `read:org` exits 2 saying `gh auth refresh -s read:org`
- S-006-2: a repo is gated when `.github/shipmill.toml` exists on its default branch through the contents API; archived repos are skipped, a 404 is not gated, and any other status exits 2 naming the repo
- S-006-3: `--repos` replaces the discovery of repos, and a listed repo outside the administered accounts exits 2 naming it
- S-006-4: with gated repos in one account, the plan is a private App owned by that account
- S-006-5: with gated repos in several accounts, the plan is a public App owned by the account with the most, a tie going to the checkout's `origin` owner when it is among them and otherwise to the first by name
- S-006-6: with no gated repo, the plan is a private App owned by the checkout's `origin` owner when the login administers it; otherwise, without `--owner`, the command exits 2 saying to pass `--owner`
- S-006-7: `--owner` and `--public`/`--private` override the plan; an `--owner` the login doesn't administer exits 2 naming it and the role needed; `--private` with gated repos outside the owner prints a warning naming each
- S-006-8: the plan is printed before anything is created, and `--dry-run` prints it, starts no server, opens no browser, and writes nothing
- S-006-9: the manifest's permissions are exactly `PERMISSIONS` from `src/shipmill/app.py`, its webhook is inactive with no events, its `public` and `name` come from the plan, and it is posted to the org's or the user's new-App URL as the owner type requires
- S-006-10: the server listens on `127.0.0.1` only, and a callback with a missing or wrong `state` writes nothing and leaves the flow waiting
- S-006-11: the code is exchanged through `POST /app-manifests/<code>/conversions`; the key is written to `~/.config/shipmill/app-<id>.pem` with mode `0600` in a directory with mode `0700`, an existing key file is never overwritten, and the client secret, webhook secret, client id, and key never appear in output
- S-006-12: with no callback in 10 minutes the command exits 2 with `no App was created`
- S-006-13: after creation the command prints the installation URL, prints `installed on <owner/repo>` as each gated repo's installation appears, and exits 1 naming the missing repos if any is still missing after 10 minutes
- S-006-14: the command never edits a repo's config; it prints the `app_id` line to add and the gate's dry run to prove it
- S-006-15: `--json` prints one object with the plan's `owner`, `owner_type`, `public`, `name`, `reason`, and `repos`, and after a real run `app_id`, `slug`, `key`, and `installed`
- S-006-16: `docs/install.md` and `skills/shipmill-setup/SKILL.md` document `shipmill app-create`, the plan's rules, and `--dry-run`, and keep the manual steps as the fallback

## Out of scope

- Editing `app_id` into the repos' configs, or opening their pull requests: the command prints the line; the setup skill or the maintainer commits it
- Installing without a browser: GitHub's API can't install an App on a user's repos for them
- One shared App for every shipmill user, with a hosted service minting tokens: a key can't be shared safely, so each user makes their own App
- Changing an existing App's permissions or visibility: GitHub's settings page does that, and the gate's checks (S-004-5) name what is missing
- Rotating or deleting a key
- Several Apps, one per account, in one run: run the command once per `--owner`

## Decisions relied on

- D-4
- D-14

## Issues

- shipmill/shipmill#155: S-006-1, S-006-2, S-006-3, S-006-4, S-006-5, S-006-6, S-006-7, S-006-8, S-006-9, S-006-10, S-006-11, S-006-12, S-006-13, S-006-14, S-006-15, S-006-16

## Verification

Checked on main plus #155's PR. GitHub's App API is checked by the tests' fake; the
discovery and the plan also ran against GitHub for real (`shipmill app-create --dry-run
--json` on 2026-10-06), and the server and callback run for real on 127.0.0.1 in the tests:

- S-006-1: `test_s006_1_the_accounts_are_the_login_and_the_orgs_it_administers` and `test_s006_1_without_read_org_the_command_says_how_to_grant_it`, passing; the real dry run found romamo and the three orgs it administers, not theagenttimes, where it is a member
- S-006-2: `test_s006_2_a_repo_is_gated_when_its_config_exists_and_archived_ones_are_skipped` and `test_s006_2_any_other_status_names_the_repo`, passing; the real dry run found cli-agent-spec/cli-agent-spec and shipmill/shipmill
- S-006-3: `test_s006_3_repos_replaces_discovery_and_refuses_another_account`, passing
- S-006-4: `test_s006_4_repos_in_one_account_plan_a_private_app_there`, passing
- S-006-5: `test_s006_5_repos_in_several_accounts_plan_a_public_app_under_the_most`, passing; the real dry run planned `shipmill-agent under shipmill, public`, the tie going to this checkout's owner
- S-006-6: `test_s006_6_no_gated_repo_plans_private_under_the_checkouts_owner_or_asks`, passing
- S-006-7: `test_s006_7_flags_override_the_plan_and_a_private_app_warns_about_other_accounts`, passing
- S-006-8: `test_s006_8_a_dry_run_prints_the_plan_and_creates_nothing`, passing; the real dry run made no POST and wrote no key
- S-006-9: `test_s006_9_the_manifest_holds_exactly_the_gates_permissions_and_no_webhook`, passing; the manifest's permissions come from `PERMISSIONS` in `src/shipmill/app.py`
- S-006-10: `test_s006_10_the_server_is_local_and_a_forged_callback_is_refused`, passing; it loads the real page, reads the state from the form, and gets 400 for a forged callback
- S-006-11: `test_s006_11_the_key_is_saved_0600_in_0700_and_the_secrets_never_appear`, passing
- S-006-12: `test_s006_12_no_callback_means_no_app`, passing
- S-006-13: `test_s006_13_installations_are_reported_as_they_appear`, `test_s006_13_polling_stops_when_the_time_runs_out`, and `test_s006_13_the_command_exits_1_naming_a_repo_left_uninstalled`, passing
- S-006-14: `test_s006_14_a_real_run_prints_the_app_id_line_and_edits_no_config`, passing; it checks the checkout's files are unchanged after a run
- S-006-15: `test_s006_15_json_on_a_dry_run_is_the_plan_alone`, and the full object after a run in `test_s006_14_a_real_run_prints_the_app_id_line_and_edits_no_config`, passing
- S-006-16: `test_s006_16_the_docs_document_app_create`, passing; read install.md and the setup skill, the manual steps kept after the command
