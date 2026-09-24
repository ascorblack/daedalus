"""Terminals: the host's side of the terminal daemons.

A terminal daemon (``ptyd``) runs one per environment — the terminals container, the operator's own
machine — and owns the pseudo-terminals. This package speaks its protocol (``wire``, ``client``),
finds it (``endpoint``), mirrors what it runs into the database and answers for owners, the audit and
the machine-wide cap (``service``), and estimates what more terminals would cost (``load``). The
HTTP routes live in the API extension, the one module allowed the web framework; everything they do
is here.
"""
