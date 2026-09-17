"""The voices the operator can choose from for speech synthesis, and what is known about each.

The same shape as :mod:`daedalus.speech.catalog` and for the same reasons: a fixed list, one archive
per entry, downloaded on demand into the state directory and never vendored. Every archive here comes
from the sherpa-onnx model zoo's ``tts-models`` release — the engine's own project — and every one of
them carries a published sha256, so unlike the recognition catalog there is no entry verified by size
and a file list alone.

A voice is not a model in the way a recogniser is. Nobody compares two Russian voices by accuracy:
they listen to one and either want it or do not. So the card's job is to let it be heard before it is
chosen (``sample``, synthesised by the engine in the card's own language) and the two bars are
``quality`` — how natural it sounds, which is editorial — and ``speed``, which is not: it is derived
from a measured real-time factor by ``100·(1 − e^(−(1/rtf)/6))``, so a voice that renders five seconds
of speech in one second scores well above one that cannot keep up with a person talking. Two entries
here are slower than real time on an ordinary CPU and say so in their own note rather than only in a
bar: Kokoro and Piper's "high" quality are worth their cost only on a machine with cores to spare.

``kind`` is the model family, not the file layout. What the files are called and whether the voice
phonemises through espeak-ng or through a lexicon is discovered inside the unpacked directory by
:func:`daedalus.speech.tts_engine.resolve`, exactly as the recognition catalog leaves file names to
the engine. That is what lets one entry be a Piper voice with ``espeak-ng-data/`` beside it and
another be a Piper voice with ``lexicon.txt`` and three rule FSTs, with nothing in this file saying so.

Licences are the dataset's, taken from each voice's own model card rather than from Piper, whose code
is MIT regardless of what it was trained on. Two of them are non-commercial and one is unknown; the
picker shows the string as it is, because a voice whose licence the operator has to think about is not
the same thing as one they do not.
"""

from __future__ import annotations

from dataclasses import dataclass

ZOO = "https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models"
"""Where every archive below comes from."""

Kind = str
"""How the engine loads a voice. One of:

``vits``    a single Piper/VITS ``.onnx`` with a tokens file, phonemised by espeak-ng or a lexicon.
``kokoro``  Kokoro's model plus a ``voices.bin`` of style vectors, one per named speaker.
``kitten``  KittenTTS, the same two-file shape as Kokoro and a different runtime.
"""

SAMPLES: dict[str, str] = {
    "ru": "Привет! Вот как звучит этот голос. Сегодня 17 сентября, и у нас 3 задачи.",
    "en": "Hello. This is what this voice sounds like. It is the 17th of September, and 3 things are waiting.",
    "de": "Hallo. So klingt diese Stimme. Heute ist der 17. September, und 3 Dinge warten.",
    "fr": "Bonjour. Voici à quoi ressemble cette voix. Nous sommes le 17 septembre, et 3 choses vous attendent.",
    "es": "Hola. Así suena esta voz. Hoy es 17 de septiembre, y hay 3 cosas esperando.",
    "it": "Ciao. Ecco come suona questa voce. Oggi è il 17 settembre, e ci sono 3 cose in attesa.",
    "pl": "Cześć. Tak brzmi ten głos. Dziś jest 17 września, a 3 rzeczy czekają.",
    "pt": "Olá. É assim que esta voz soa. Hoje é 17 de setembro, e 3 coisas esperam por você.",
    "uk": "Привіт! Ось як звучить цей голос. Сьогодні 17 вересня, і на тебе чекають 3 справи.",
    "zh": "你好，这就是这个声音。今天是九月十七日，还有三件事在等着你。",
}
"""What a card plays when the operator asks to hear a voice, in the voice's own language.

Each one carries a digit and a date on purpose. Piper phonemises through espeak-ng, which expands
numbers in the language it is speaking, and the whole question an operator has about a synthesiser is
whether it says "семнадцатое сентября" or spells out the characters. A sample that never contains a
number answers a question nobody asked."""

FALLBACK_SAMPLE = SAMPLES["en"]
"""For a language with no sample of its own; the voice will read it in its own accent, which is still
more use than silence."""


@dataclass(frozen=True)
class TtsVoice:
    """One downloadable voice, exactly as the picker shows it."""

    id: str
    """Stable, short, ours. The config and the installed directory are named by it, so it never changes."""
    label: str
    archive: str
    """Basename of the ``.tar.bz2`` in the zoo release; the download URL is ``ZOO`` plus this."""
    kind: Kind
    language: str
    """ISO 639-1. One language per voice — a synthesiser speaks the language it was trained on, and a
    voice reading another one is a party trick rather than a feature."""
    gender: str
    """``female``, ``male`` or ``mixed`` (a multi-speaker archive holding both)."""
    size_bytes: int
    """The archive as downloaded, for the progress bar and the disk estimate."""
    unpacked_bytes: int
    """What it occupies once unpacked — measured, not guessed. About nineteen megabytes of every Piper
    entry is the espeak-ng data each archive carries its own copy of, which is also why nothing extra
    has to be installed for one to speak."""
    memory_mb: int
    """Roughly what the loaded voice adds to the process."""
    sample_rate: int
    """What it synthesises at. The audio is sent on at this rate rather than resampled: a browser plays
    any of these, and resampling a synthesiser's own output is only a worse copy of it."""
    licence: str
    """The dataset's licence, as its model card states it — not Piper's."""
    quality: int
    speed: int
    rtf: float
    """Measured real-time factor on an ordinary desktop CPU with two threads: seconds of work per
    second of speech. Below 1 is faster than talking. ``speed`` is derived from it; this is the number
    it was derived from, kept so the derivation can be checked."""
    note: str
    """One line of UI copy: who this voice is for, in the operator's terms."""
    sha256: str
    speakers: tuple[str, ...] = ()
    """Named speakers inside a multi-speaker archive, in the order the model numbers them. Empty for a
    single-voice model. The names come from the model's own ONNX metadata; ``a``/``b`` is the accent
    (American, British) and ``f``/``m`` the voice, which is why the picker can show a gender per entry."""
    recommended_for: tuple[str, ...] = ()
    """Languages this is the suggested voice for. Per language, like the recognition catalog: the best
    English voice and the best Russian one are different files and one flag cannot say so."""

    @property
    def url(self) -> str:
        return f"{ZOO}/{self.archive}"

    @property
    def contents(self) -> tuple[str, ...]:
        """Nothing is pinned by file list here: every archive in this catalog has a published digest.

        The attribute exists because the download manager is shared with the recognition catalog and
        asks every entry the same questions."""
        return ()

    @property
    def multi(self) -> bool:
        return len(self.speakers) > 1

    @property
    def keeps_up(self) -> bool:
        """Whether it synthesises faster than a person speaks. False is usable and is not a failure —
        it means the first sentence of an answer is heard later than it was written."""
        return self.rtf < 1.0

    def sample(self) -> str:
        return SAMPLES.get(self.language, FALLBACK_SAMPLE)

    def speaks(self, language: str) -> bool:
        """Whether this voice is in a language. Empty or ``"any"`` asks nothing of it."""
        if not language or language == "any":
            return True
        return self.language == language.split("-")[0].lower()


VOICES: tuple[TtsVoice, ...] = (
    # -- Russian ------------------------------------------------------------------------------
    TtsVoice(
        id="ru-dmitri",
        label="Dmitri (Russian)",
        archive="vits-piper-ru_RU-dmitri-medium-int8.tar.bz2",
        kind="vits",
        language="ru",
        gender="male",
        size_bytes=21_129_441,
        unpacked_bytes=36_577_368,
        memory_mb=120,
        sample_rate=22_050,
        licence="CC0 (public domain)",
        quality=74,
        speed=53,
        rtf=0.219,
        note="A clear male Russian, four times faster than speech, and the only one here whose dataset is public domain.",
        sha256="7636793307f634ce54c6e65528a91a61683114f1a6635a08caf64ba6c54e6a63",
        recommended_for=("ru",),
    ),
    TtsVoice(
        id="ru-irina",
        label="Irina (Russian)",
        archive="vits-piper-ru_RU-irina-medium-int8.tar.bz2",
        kind="vits",
        language="ru",
        gender="female",
        size_bytes=21_149_417,
        unpacked_bytes=36_577_296,
        memory_mb=120,
        sample_rate=22_050,
        licence="dataset licence unknown (RHVoice)",
        quality=73,
        speed=39,
        rtf=0.337,
        note="The female Russian voice. Warm and unhurried; its training data carries no stated licence.",
        sha256="b0000a509f7551a80742eed5c43b8eb03f469cb9f0a42f6feee96ce0da0ebab8",
    ),
    TtsVoice(
        id="ru-ruslan",
        label="Ruslan (Russian)",
        archive="vits-piper-ru_RU-ruslan-medium-int8.tar.bz2",
        kind="vits",
        language="ru",
        gender="male",
        size_bytes=21_127_907,
        unpacked_bytes=36_577_486,
        memory_mb=120,
        sample_rate=22_050,
        licence="CC BY-NC-SA 4.0 — non-commercial",
        quality=76,
        speed=49,
        rtf=0.249,
        note="The most natural Russian here, and the one you may not use commercially.",
        sha256="93b9f8c7b34c1420bdc253a0ce5ab1cb6b6be2e8fa26c1f3b36c6189233c3e7e",
    ),
    # -- English ------------------------------------------------------------------------------
    TtsVoice(
        id="en-amy",
        label="Amy (American English)",
        archive="vits-piper-en_US-amy-medium-int8.tar.bz2",
        kind="vits",
        language="en",
        gender="female",
        size_bytes=21_028_122,
        unpacked_bytes=36_679_476,
        memory_mb=120,
        sample_rate=22_050,
        licence="Mimic-3 voices — see the dataset",
        quality=72,
        speed=57,
        rtf=0.195,
        note="Twenty megabytes, five times faster than speech, and it never makes the conversation wait.",
        sha256="bd23c0aa629eb3719448582f45ede49e8fa6a679061fed5eab16a6a6fd8e7e82",
        recommended_for=("en",),
    ),
    TtsVoice(
        id="en-alba",
        label="Alba (Scottish English)",
        archive="vits-piper-en_GB-alba-medium-int8.tar.bz2",
        kind="vits",
        language="en",
        gender="female",
        size_bytes=21_104_326,
        unpacked_bytes=36_679_538,
        memory_mb=120,
        sample_rate=22_050,
        licence="CC BY 4.0",
        quality=72,
        speed=49,
        rtf=0.249,
        note="A Scottish voice, for an English that is not American. Same size and much the same speed as Amy.",
        sha256="f7581d123ae977f64f3032bb247d4deeac8440e881d918d36fdd36d8f1030fb7",
    ),
    TtsVoice(
        id="en-ryan",
        label="Ryan (American English, high)",
        archive="vits-piper-en_US-ryan-high-int8.tar.bz2",
        kind="vits",
        language="en",
        gender="male",
        size_bytes=34_473_341,
        unpacked_bytes=61_751_849,
        memory_mb=180,
        sample_rate=22_050,
        licence="CC BY-NC-SA 4.0 — non-commercial",
        quality=82,
        speed=12,
        rtf=1.341,
        note="Piper at its best, and slower than talking on an ordinary processor: every sentence is heard a beat late.",
        sha256="df13fc140db80fd586e98834cad703a8988c1004748daa068dd07a59e502013e",
    ),
    TtsVoice(
        id="en-kitten",
        label="KittenTTS Nano (English, 8 voices)",
        archive="kitten-nano-en-v0_8-int8.tar.bz2",
        kind="kitten",
        language="en",
        gender="mixed",
        size_bytes=31_220_690,
        unpacked_bytes=45_652_547,
        memory_mb=130,
        sample_rate=24_000,
        licence="Apache-2.0",
        quality=64,
        speed=47,
        rtf=0.261,
        note="Eight English voices, four male and four female, in one small download. Plainer than Piper, and there is choice in it.",
        sha256="6fa5be852612ce761094ba74ee6123b4fc4acfefa79bf64dc63acae4a83af2fd",
        speakers=(
            "expr-voice-2-m", "expr-voice-2-f", "expr-voice-3-m", "expr-voice-3-f",
            "expr-voice-4-m", "expr-voice-4-f", "expr-voice-5-m", "expr-voice-5-f",
        ),
    ),
    TtsVoice(
        id="en-kokoro",
        label="Kokoro (English, 11 voices)",
        archive="kokoro-int8-en-v0_19.tar.bz2",
        kind="kokoro",
        language="en",
        gender="mixed",
        size_bytes=103_248_205,
        unpacked_bytes=157_947_103,
        memory_mb=320,
        sample_rate=24_000,
        licence="Apache-2.0",
        quality=92,
        speed=13,
        rtf=1.203,
        note="The best-sounding English here by a distance, and slower than real time: it suits a machine with cores to spare.",
        sha256="c9f0dd393615805b0bab050c340834d5e684e732aec91c0e860cd30e982c08bd",
        speakers=(
            "af", "af_bella", "af_nicole", "af_sarah", "af_sky", "am_adam",
            "am_michael", "bf_emma", "bf_isabella", "bm_george", "bm_lewis",
        ),
    ),
    # -- everywhere else ----------------------------------------------------------------------
    TtsVoice(
        id="de-thorsten",
        label="Thorsten (German)",
        archive="vits-piper-de_DE-thorsten-medium-int8.tar.bz2",
        kind="vits",
        language="de",
        gender="male",
        size_bytes=20_949_833,
        unpacked_bytes=36_577_367,
        memory_mb=120,
        sample_rate=22_050,
        licence="CC0 (public domain)",
        quality=76,
        speed=60,
        rtf=0.183,
        note="The German voice most German projects use, and the fastest entry in this list.",
        sha256="07e240b7b9c1fc9211d5a69512f8cbe11b3286c2ed79c15c076ac6ed427fdf13",
        recommended_for=("de",),
    ),
    TtsVoice(
        id="fr-siwis",
        label="Siwis (French)",
        archive="vits-piper-fr_FR-siwis-medium-int8.tar.bz2",
        kind="vits",
        language="fr",
        gender="female",
        size_bytes=20_914_888,
        unpacked_bytes=36_577_449,
        memory_mb=120,
        sample_rate=22_050,
        licence="CC BY 4.0",
        quality=73,
        speed=59,
        rtf=0.189,
        note="A steady French, quick enough that nothing waits for it.",
        sha256="3909cff9b3cfd4820c66aa13bf554315c82e34899c161f0b446ece372bc4b5ec",
        recommended_for=("fr",),
    ),
    TtsVoice(
        id="es-davefx",
        label="DaveFX (Spanish)",
        archive="vits-piper-es_ES-davefx-medium-int8.tar.bz2",
        kind="vits",
        language="es",
        gender="male",
        size_bytes=21_171_632,
        unpacked_bytes=36_577_368,
        memory_mb=120,
        sample_rate=22_050,
        licence="CC0 (public domain)",
        quality=74,
        speed=55,
        rtf=0.211,
        note="Castilian Spanish, public domain, and as cheap to run as the rest of the Piper voices.",
        sha256="8bb8ac1cefb727caec9bd9c6c3185c673c8b42c53bd29bb25d5a7715dac37125",
        recommended_for=("es",),
    ),
    TtsVoice(
        id="it-paola",
        label="Paola (Italian)",
        archive="vits-piper-it_IT-paola-medium-int8.tar.bz2",
        kind="vits",
        language="it",
        gender="female",
        size_bytes=21_143_212,
        unpacked_bytes=36_584_266,
        memory_mb=120,
        sample_rate=22_050,
        licence="see the dataset",
        quality=73,
        speed=52,
        rtf=0.227,
        note="Italian, in the same twenty megabytes everything else in this family takes.",
        sha256="2b975ed305391c056944a4dde67ee754dd824099503a860295bb4c1d724662d8",
        recommended_for=("it",),
    ),
    TtsVoice(
        id="pl-gosia",
        label="Gosia (Polish)",
        archive="vits-piper-pl_PL-gosia-medium-int8.tar.bz2",
        kind="vits",
        language="pl",
        gender="female",
        size_bytes=21_109_262,
        unpacked_bytes=36_679_415,
        memory_mb=120,
        sample_rate=22_050,
        licence="see the dataset",
        quality=72,
        speed=51,
        rtf=0.233,
        note="Polish, clear and unornamented.",
        sha256="72acac4c4b031725c41a61b3af0314a3d30e1ec2cd83ee410ea5f9e6d2d9d4fb",
        recommended_for=("pl",),
    ),
    TtsVoice(
        id="pt-faber",
        label="Faber (Brazilian Portuguese)",
        archive="vits-piper-pt_BR-faber-medium-int8.tar.bz2",
        kind="vits",
        language="pt",
        gender="male",
        size_bytes=21_336_772,
        unpacked_bytes=36_679_473,
        memory_mb=120,
        sample_rate=22_050,
        licence="see the dataset",
        quality=73,
        speed=60,
        rtf=0.180,
        note="Brazilian Portuguese, and one of the two quickest voices here.",
        sha256="dbc8b1d7d729fd417ea78a350ed35696c928770ac93513d3f507bd4e88eee3fd",
        recommended_for=("pt",),
    ),
    TtsVoice(
        id="uk-lada",
        label="Lada (Ukrainian)",
        archive="vits-piper-uk_UA-lada-x_low-int8.tar.bz2",
        kind="vits",
        language="uk",
        gender="female",
        size_bytes=13_332_976,
        unpacked_bytes=25_852_839,
        memory_mb=90,
        sample_rate=16_000,
        licence="see the dataset",
        quality=58,
        speed=77,
        rtf=0.112,
        note="Thirteen megabytes of Ukrainian at sixteen kilohertz. Rougher than the rest, and nearly free to run.",
        sha256="5ba331ec01c811d605951c30ad7f9f57dfe87aaaefb9295c83012867db14a91a",
        recommended_for=("uk",),
    ),
    TtsVoice(
        id="zh-xiaoya",
        label="Xiao Ya (Mandarin Chinese)",
        archive="vits-piper-zh_CN-xiao_ya-medium-int8.tar.bz2",
        kind="vits",
        language="zh",
        gender="female",
        size_bytes=14_016_124,
        unpacked_bytes=20_933_412,
        memory_mb=100,
        sample_rate=22_050,
        licence="non-commercial (Data Baker BZNSYP)",
        quality=74,
        speed=52,
        rtf=0.226,
        note="Mandarin. It phonemises through a lexicon rather than espeak-ng, so it is the smallest download here.",
        sha256="eab027e194e70289233cf12308373611fa4e2e96ef2d97354ef433ab663d831f",
        recommended_for=("zh",),
    ),
)

BY_ID: dict[str, TtsVoice] = {v.id: v for v in VOICES}


def get(voice_id: str) -> TtsVoice:
    """The entry with this id, or ``KeyError`` naming what is actually on offer."""
    try:
        return BY_ID[voice_id]
    except KeyError:
        raise KeyError(f"no voice {voice_id!r}; the catalog has {', '.join(sorted(BY_ID))}") from None


def languages() -> list[str]:
    """Every language a voice speaks, sorted, for the picker's filter."""
    return sorted({voice.language for voice in VOICES})


def recommended(language: str = "") -> TtsVoice | None:
    """The suggested voice for a language, or None where the catalog has nothing in it."""
    wanted = (language or "").split("-")[0].lower()
    for voice in VOICES:
        if wanted and wanted in voice.recommended_for:
            return voice
    return None


def as_json(voice: TtsVoice) -> dict[str, object]:
    """One entry as the Mini App reads it."""
    return {
        "id": voice.id,
        "label": voice.label,
        "kind": voice.kind,
        "language": voice.language,
        "gender": voice.gender,
        "size_bytes": voice.size_bytes,
        "disk_bytes": voice.unpacked_bytes,
        "memory_mb": voice.memory_mb,
        "sample_rate": voice.sample_rate,
        "licence": voice.licence,
        "quality": voice.quality,
        "speed": voice.speed,
        "rtf": voice.rtf,
        "keeps_up": voice.keeps_up,
        "note": voice.note,
        "url": voice.url,
        "speakers": list(voice.speakers),
        "recommended_for": list(voice.recommended_for),
    }


__all__ = [
    "BY_ID",
    "FALLBACK_SAMPLE",
    "SAMPLES",
    "VOICES",
    "ZOO",
    "TtsVoice",
    "as_json",
    "get",
    "languages",
    "recommended",
]
