
"""Ask Jenny application package.

The package is split into a thin HTTP layer in :mod:`app.main`, domain services
for ingestion, retrieval, chat, and watched folders, and persistence adapters in
:mod:`app.datastore`.  Keeping this file free of startup work makes importing
individual modules safe in tests and command-line utilities.
"""
