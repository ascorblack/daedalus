"""Where the stress falls in a Russian word, for the one synthesiser that can be told.

Russian is written without stress marks and a synthesiser has to guess, which is the single thing
that makes a Russian voice sound foreign: «прив+ет» said as «пр+ивет» is not an accent, it is a
mistake. Piper guesses through espeak-ng's rules and gets a word wrong now and then. Supertonic
learned stress from data and also **honours an explicit mark**: a combining acute (U+0301) after the
stressed vowel moves the stress, measurably and in the direction asked for. That is what this module
exists to put there.

It is a dictionary and nothing else — no model, no training, no wheel, no megabytes. The entries are
written with a ``+`` before the stressed vowel, which is how a Russian dictionary writes it and the
only form of this table a reader can check by eye. A word that is not in it is left exactly as it was
written, so the worst this can do to an unknown word is nothing.

**Only where it is certain.** A wordform that is two words in writing — «за́мок» and «замо́к» are the
same six letters — cannot be resolved without reading the sentence, and guessing there would replace
a stress the model sometimes gets right with one that is wrong half the time by construction. Those
forms are named in :data:`HOMOGRAPHS` and are never marked; a form listed twice with two different
stresses is treated the same way, so the table cannot contradict itself into a guess. A word with one
vowel has nothing to decide and is left alone too.

The table is small on purpose. It carries the words an assistant says every day rather than a
language: a few hundred entries cost nothing to look up and can be read through by a person who
knows the language, and the ones that matter most — the greeting a conversation opens with — are
worth more than coverage of a dictionary nobody will check.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - types only
    from daedalus.speech.tts_catalog import TtsVoice

STRESS = "́"
"""Combining acute accent. It goes **after** the vowel it stresses, as a combining mark does."""

MARK = "+"
"""What the table below writes instead, in front of the stressed vowel. Never sent to a model."""

VOWELS = "аеёиоуыэюя"

WORD = re.compile(f"[а-яёА-ЯЁ{STRESS}]+")
"""One Russian word. Cyrillic only: a Latin word inside a Russian sentence is not ours to mark, and
neither is a digit — Supertonic expands those itself, in the language it is reading.

The acute is part of a word rather than a break in one, or text that has already been marked would
match as the two halves either side of the mark, and the first half — a whole word, ending in the
vowel the mark belongs to — would be looked up and marked a second time."""

WORDS = """
    прив+ет здр+авствуйте спас+ибо пож+алуйста извин+ите прощ+айте пок+а
    д+оброе +утро в+ечер +очень
    сег+одня вчер+а з+автра сейч+ас теп+ерь ч+ерез п+осле р+аньше п+озже
    всегд+а никогд+а иногд+а ч+асто р+едко ск+оро д+олго оп+ять сн+ова
    мин+ута мин+уты мин+ут мин+уту сек+унда сек+унды час+ы нед+еля нед+ели
    м+есяц м+есяца год+ами вр+емя вр+емени с+утки
    понед+ельник вт+орник четв+ерг п+ятница субб+ота воскрес+енье
    янв+арь февр+аль апр+ель авг+уст сент+ябрь окт+ябрь но+ябрь дек+абрь
    зад+ача зад+ачи зад+ачу зад+ачей раб+ота раб+оту раб+оты раб+отает
    вопр+ос вопр+оса вопр+осы отв+ет отв+ета отв+еты прим+ер прим+ера
    прич+ина прич+ины реш+ение реш+ения знач+ение знач+ения
    результ+ат результ+ата результ+аты ош+ибка ош+ибки ош+ибок
    прог+он прог+она з+апуск з+апуска пров+ерка пров+ерки нач+ало кон+ец
    файл+ы п+апка п+апке стран+ица стран+ицы сс+ылка сс+ылки
    сообщ+ение сообщ+ения письм+о звон+ок встр+еча встр+ечи
    про+ект про+екта про+екты сист+ема сист+емы с+ервер с+ервера
    конфигур+ация конфигур+ацию конфигур+ации настр+ойка настр+ойки
    в+ерсия в+ерсии внутр+и снар+ужи
    т+есты т+еста т+естов тайм+аут тайм+аута
    пров+ерил пров+ерила пров+ерить пров+ерю попр+авил попр+авить
    перезапуст+ил перезапуст+ить запуст+ил запуст+ить останов+ил останов+ить
    сд+елал сд+елала сд+елать напис+ал напис+ать посмотр+ел посмотр+еть
    отпр+авил отпр+авить получ+ил получ+ить н+адо н+ужно м+ожно нельз+я
    пад+али уп+ало раб+отало сраб+отало
    скаж+у говор+ю говор+ит зн+аю зн+ает д+умаю д+умает
    в+ижу в+идел сл+ышу пон+ял понял+а пон+яли п+омню п+омнит
    мог+у м+ожет м+огут х+очет хот+ел хот+ела б+уду б+удет б+удут
    нач+ал нач+ать зак+ончил зак+ончить гот+ово гот+ов
    п+ару п+ары чет+ыре в+осемь д+евять д+есять
    п+ервый втор+ой тр+етий посл+едний сл+едующий
    хорош+о пл+охо б+ыстро м+едленно пр+осто сл+ожно легк+о т+очно в+ерно
    им+енно кон+ечно нав+ерное возм+ожно об+ычно почт+и совс+ем т+олько
    т+акже т+оже по+этому пот+ому зат+ем спр+ава сл+ева
    куд+а отк+уда почем+у зач+ем ск+олько как+ой как+ая
    +этот +эта +это +эти сам+а с+ами
    мен+я теб+я ег+о сво+я мо+я тво+я н+аши в+аши
    больш+ой больш+ая м+аленький н+овый ст+арый хор+оший плох+ой
    в+ажный н+ужный гот+овый откр+ытый закр+ытый
    прост+ой б+ыстрый м+едленный компь+ютер прогр+амма прогр+аммы разд+ел
    """
"""Every wordform this knows, with a ``+`` in front of the vowel that carries the stress.

Written as a block rather than a mapping because it is a list to read, not a structure to navigate,
and because adding a word should cost one word. A form that appears twice with two different stresses
is thrown out at import rather than resolved: the table has then said it does not know."""

HOMOGRAPHS = frozenset(
    """
    замок мука дома потом уже полки села слова воды горы руки ноги головы
    стороны доски вести писать парить духи атлас орган пропасть хлопок белки
    козлы кружки плачу видение бегом уже ещё письма начала среда года
    """.split()
)
"""Wordforms that are two words in writing, and are therefore never marked.

They are listed even though none of them is in :data:`WORDS`, because the check that matters is the
one that survives somebody adding a word in a hurry."""


def _table() -> dict[str, int]:
    """The wordlist as ``plain form -> index of the stressed vowel``, contradictions dropped."""
    out: dict[str, int] = {}
    dropped: set[str] = set()
    for entry in WORDS.split():
        at = entry.find(MARK)
        plain = entry.replace(MARK, "")
        if at < 0 or at >= len(plain) or plain[at] not in VOWELS:
            # A malformed row is the table lying about a word; it says nothing about it instead.
            dropped.add(plain)
            continue
        if plain in out and out[plain] != at:
            dropped.add(plain)
        out[plain] = at
    for plain in dropped | set(HOMOGRAPHS):
        out.pop(plain, None)
    return out


TABLE: dict[str, int] = _table()
"""What :func:`mark` looks a word up in."""


def _stressed(word: str) -> str:
    """One word with the acute put in, or the word exactly as it came."""
    lower = word.lower()
    if "ё" in lower or STRESS in word:
        # «ё» is always the stressed vowel and is already written as one; a word that arrived marked
        # was marked by whoever wrote it, and they meant it.
        return word
    if sum(ch in VOWELS for ch in lower) < 2:
        return word
    at = TABLE.get(lower)
    if at is None:
        return word
    return word[: at + 1] + STRESS + word[at + 1 :]


def mark(text: str) -> str:
    """Russian text with the stress written in wherever it is known and unambiguous.

    Idempotent: a word that already carries an acute is left alone, so text that has been through
    here twice is the same text.
    """
    return WORD.sub(lambda m: _stressed(m.group(0)), text)


def applies(voice: TtsVoice) -> bool:
    """Whether marking is worth doing for this voice at all.

    Two conditions, and both are narrow on purpose. The family has to be one that reads the mark —
    Supertonic does; espeak-ng, which every Piper voice phonemises through, would be handed a
    character it has no rule for, and a voice that speaks today must not be made to stumble for a
    voice that does not. And the entry has to be a Russian one: the same Cyrillic letters are
    Ukrainian and Bulgarian words with their own stress, and this table is not about them.
    """
    return voice.kind == "supertonic" and voice.language == "ru"


__all__ = ["HOMOGRAPHS", "MARK", "STRESS", "TABLE", "WORDS", "applies", "mark"]
