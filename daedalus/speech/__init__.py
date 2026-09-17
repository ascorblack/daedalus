"""Speech recognition that runs here, on the CPU, with no endpoint and no key.

Three modules, in the order a model travels through them: :mod:`catalog` is the fixed list the
operator chooses from, :mod:`models` downloads and keeps what was chosen, and :mod:`engine` loads it
and turns audio into words. Nothing here is imported at start-up — the engine's wheel is an optional
extra, so every entry point that needs it imports it inside the function that uses it and says so
plainly when it is missing.
"""
