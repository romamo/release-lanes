### Fixed

- An answered needs-decision issue reads DECIDED in `triage_state.py`, an action the gate starts a session for on the next tick instead of after its retry window, and a hold word in one sentence of a comment line no longer holds the issue on a link another sentence names (#309)
