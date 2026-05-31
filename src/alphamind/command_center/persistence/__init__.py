"""Command-center persistence layer.

Three modules:

* :mod:`alphamind.command_center.persistence.tables` — SQLAlchemy
  declarative tables for the three command-center-owned tables.
* :mod:`alphamind.command_center.persistence.codecs` — row ↔ frozen
  dataclass converters (parse-don't-validate boundary; downstream code
  operates on the dataclasses, not the rows).
* :mod:`alphamind.command_center.persistence.session` — dual session
  factories: ``cc_writer_session`` (writes scoped to the three owned
  tables via a separate :class:`~sqlalchemy.orm.DeclarativeBase`) and
  ``foreign_reader_session`` (read-only against the rest of the DB via
  ``?mode=ro`` in the URI).
"""
