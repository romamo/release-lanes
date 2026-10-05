class ReleaseError(Exception):
    """Bad input: a malformed policy, version, or CHANGELOG, a repo the bot can't read, or a
    state it refuses to act on. The CLI exits 2 with the message."""
