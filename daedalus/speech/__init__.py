"""Speech that runs here, on the CPU, with no endpoint and no key — both directions of it.

Two halves with the same shape. Recognition is :mod:`catalog`, the fixed list the operator chooses a
model from, and :mod:`engine`, which loads one and turns audio into words. Synthesis is
:mod:`tts_catalog` and :mod:`tts_engine`, which do the same for voices and turn words back into audio.
Between them sits :mod:`models`, the one download manager both use: a model and a voice are fetched,
resumed, verified, unpacked and deleted identically, so which catalog an id belongs to is an argument
rather than a second copy of the file. :mod:`service` and :mod:`tts_service` are how the application
asks each half for something.

Nothing here is imported at start-up. The engine's wheel is an optional extra — the same wheel for
both halves — so every entry point that needs it imports it inside the function that uses it and says
plainly when it is missing.
"""
