"""The browser: the host's side of the browser daemons.

A browser daemon (``browserd``) runs one per environment — its own compose service, or a child of the
desktop launcher — and owns Chromium, its profiles, tabs and pixels (``docs/architecture/browser.md``).
This package speaks its protocol over the terminal daemon's framing (``wire``, and the shared client),
mirrors the groups it runs for each agent into the database, holds the audit, and answers for owners,
the cap on running browsers, the live view's tickets and the load (``service``). The agent's tools
(``daedalus.tools.browser``) and the command-line staff's copies of them (``agent``) go through it;
the HTTP routes live in an API extension, the one kind of module allowed the web framework.
"""
