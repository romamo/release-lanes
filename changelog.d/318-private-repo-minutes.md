### Fixed

- `shipmill settle-minutes` prints 0 when the policy's mode is `off`, so the settle job no longer sleeps billed runner minutes after every push for a run that then skips; the release lanes reference adds the cost on private repositories, and shipmill-setup reads the repo's visibility and, on a private repo, offers the cheaper setups with rough monthly figures before choosing lanes (#318)
