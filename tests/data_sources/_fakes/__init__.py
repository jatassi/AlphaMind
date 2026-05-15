"""In-memory fakes implementing each vendor Protocol.

These fakes replace mock-library doubles in tests/data_sources/ —
sociable testing per the architecture audit's "mock what you don't own"
(P8) finding.

Each `Fake<Vendor>API` dataclass implements the corresponding Protocol in
`src/alphamind/data_sources/<vendor>/_protocol.py`. Tests construct the
fake with literal data; the fake owns the data shape rather than the
test setting return values on a generic double.
"""
