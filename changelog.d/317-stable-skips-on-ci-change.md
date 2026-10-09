### Fixed

- A stable promotion skips when `.github/workflows/` changed since the rc it would promote, since the release's CI runs main's workflows on the rc's code, and the same run cuts the next rc instead, which carries the new CI and soaks before it is promoted (#317)
