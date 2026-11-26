"""
capture.py - takes one screenshot of a fixed screen region and reads the text in it.

No network here: this file only looks at the screen and runs the OCR engine.
"""

import ctypes
import os
import re
import shutil

import mss
import pytesseract
from PIL import Image, ImageFilter

# ---------------------------------------------------------------- settings --
# The part of the screen to read, in screen pixels (left, top, width, height).
# Change these four numbers to fit your game's dialogue box. If your screen is
# scaled in Windows (125%, 150%...), these are still real pixels.
REGION = {"left": 108, "top": 796, "width": 1715, "height": 173}

# The languages we try, in this order. Each line is:
#     (Tesseract language names, the characters a successful read must contain)
# Japanese and English are tried first because they are quick; the rest only
# run when nothing readable was found in the earlier ones. "eng" also reads the
# Latin-script languages (French, German, Spanish, Italian, Polish...).
# Extra language data is downloaded by translator.ensure_ocr_languages().
LANGUAGE_GROUPS = (
    ("jpn+eng", r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff]"),        # Japanese
    ("eng", r"[A-Za-z]"),                                              # Latin script
    ("kor+eng", r"[\uac00-\ud7af\u1100-\u11ff]"),                      # Korean
    ("rus+eng", r"[\u0400-\u04ff]"),                                   # Russian
    ("chi_sim+eng", r"[\u3400-\u4dbf\u4e00-\u9fff]"),                  # Chinese (simplified)
    ("chi_tra+eng", r"[\u3400-\u4dbf\u4e00-\u9fff]"),                  # Chinese (traditional)
    ("ara+eng", r"[\u0600-\u06ff]"),                                   # Arabic
    ("tha+eng", r"[\u0e00-\u0e7f]"),                                   # Thai
    ("heb+eng", r"[\u0590-\u05ff]"),                                   # Hebrew
    ("ell+eng", r"[\u0370-\u03ff]"),                                   # Greek
    ("hin+eng", r"[\u0900-\u097f]"),                                   # Hindi
)

# A read counts only when Tesseract is this sure of the words. A wrong language
# pack still "finds" letters in the picture, but it is unsure of them - this is
# what stops Korean text from being read as English nonsense.
MIN_CONFIDENCE = 55

# Tesseract language data lives in this folder (tessdata/jpn.traineddata,
# eng.traineddata copied from the installer, plus packs downloaded at startup).
TESSDATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tessdata")

# Where the Tesseract installer usually puts tesseract.exe.
TESSERACT_CANDIDATES = [
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Programs\Tesseract-OCR\tesseract.exe"),
]


def enable_dpi_awareness() -> None:
    """Make screen coordinates match real pixels on scaled displays.

    Without this, Windows can silently scale every coordinate and we would
    grab the wrong part of the screen.
    """
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


_configured = False


def _tesseract_exe() -> str:
    """Where tesseract.exe is. Raises RuntimeError if the installer is missing."""
    found = shutil.which("tesseract")
    if found:
        return found
    for path in TESSERACT_CANDIDATES:
        if os.path.isfile(path):
            return path
    raise RuntimeError(
        "Tesseract OCR was not found. Install it with:  winget install UB-Mannheim.TesseractOCR"
    )


def _copy_language_data(exe: str) -> None:
    """Copy the language files the installer brought into our tessdata folder.

    Tesseract looks in a single folder for its language data, and we point it
    at ours (Japanese lives here). Copying English across means the app can
    read Latin scripts without downloading anything.
    """
    try:
        os.makedirs(TESSDATA_DIR, exist_ok=True)
    except OSError:
        return
    if not os.path.isfile(exe):
        return                                  # found on PATH, location unknown
    install_dir = os.path.join(os.path.dirname(os.path.abspath(exe)), "tessdata")
    if not os.path.isdir(install_dir):
        return
    for name in os.listdir(install_dir):
        if not name.endswith(".traineddata"):
            continue
        target = os.path.join(TESSDATA_DIR, name)
        if os.path.exists(target):
            continue
        try:
            shutil.copyfile(os.path.join(install_dir, name), target)
        except OSError:
            pass                                # read-only install or no space


def _configure_tesseract() -> None:
    """Tell pytesseract where tesseract.exe is. Raises RuntimeError if missing."""
    global _configured
    if _configured:
        return
    exe = _tesseract_exe()                      # raises the readable error
    pytesseract.pytesseract.tesseract_cmd = exe
    os.environ.setdefault("TESSDATA_PREFIX", TESSDATA_DIR)
    _copy_language_data(exe)
    _configured = True


def available_languages() -> set:
    """The language codes we have data for right now."""
    try:
        names = os.listdir(TESSDATA_DIR)
    except OSError:
        return set()
    return {name[: -len(".traineddata")] for name in names if name.endswith(".traineddata")}


def _language_available(language: str) -> bool:
    """True when every code in "jpn+eng" has its .traineddata file."""
    have = available_languages()
    return all(code in have for code in language.split("+"))


def check_tesseract() -> None:
    """Fail early with a readable message instead of on the first hotkey press."""
    _configure_tesseract()


def capture_region() -> Image.Image:
    """Take ONE screenshot of REGION and return it as a Pillow image."""
    with mss.mss() as screen:
        shot = screen.grab(REGION)
        return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")


# Everything that counts as Japanese while fixing spaces: kana, kanji, and
# Japanese punctuation/full-width forms (。、！？「」...), so "。 だから" is
# closed up instead of being joined with a stray space.
_JA = (
    r"\u3000-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uff01-\uff60\uff66-\uff9f"
)
_JA_CHAR_RE = re.compile(f"[{_JA}]")

# CJK only. Used to tell real dialogue apart from HUD bits such as
# "HP 50/50" or the edges of a portrait, which the wide capture box also sees.
_KANA_KANJI_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uff66-\uff9f]")

# A space Tesseract put between two Japanese characters: Japanese has no word
# spaces, so it is noise and would make the sentence read as chopped-up words.
_SPACE_BETWEEN_JAPANESE = re.compile(f"(?<=[{_JA}])[ \t]+(?=[{_JA}])")
# " 。" -> "。"
_SPACE_BEFORE_PUNCTUATION = re.compile(r"[ \t]+(?=[、。，．！？…‥」』）】])")
# Stray marks Tesseract likes to invent along box edges, and the "next" arrow
# (▼) the game draws after the line, which OCR turns into "・" or "マ".
_OCR_JUNK = re.compile(r"[|_\u00a6]+")
_TRAILING_NOISE = re.compile(r"[\s\u30fb\uff65\u25bc\u25b2\u25bd\u25b3\u2015\u2014_=~]+$")
_NOISE_BEFORE_PUNCTUATION = re.compile(r"[\u30fb\uff65]+(?=[\u3001\u3002])")

# ①..⑳ (and ⓪) read back as plain numbers.
_CIRCLED_DIGITS = str.maketrans(
    {chr(0x2460 + i): str(i + 1) for i in range(20)} | {chr(0x24EA): "0"}
)


def clean_ocr_text(raw: str) -> str:
    """Turn Tesseract's raw output into one clean line of dialogue.

    Two rules, because they cannot be the same rule for both scripts:
      * Japanese has no spaces, so lines that wrapped in the game box are glued
        back together and spurious spaces are removed.
      * Latin words (names, "HP", "Elixir") must keep their spaces, otherwise
        they reach the translator as "SwordofDawn" and translate badly.
    """
    lines = []
    for line in raw.splitlines():
        line = " ".join(line.split())         # collapse runs, trim the ends
        if line:
            lines.append(line)

    # The capture box is wide, so it also catches bits of the HUD ("HP 50/50"),
    # an item counter or the edge of a portrait. Those lines are what come back
    # as random words and numbers, so: if real Japanese was read, keep only the
    # lines that contain kana or kanji.
    if any(_KANA_KANJI_RE.search(line) for line in lines):
        lines = [line for line in lines if _KANA_KANJI_RE.search(line)]

    joined = ""
    for line in lines:
        if not joined:
            joined = line
        elif _JA_CHAR_RE.match(joined[-1]) and _JA_CHAR_RE.match(line[0]):
            joined += line                    # wrapped Japanese: no space
        else:
            joined += " " + line

    joined = _SPACE_BETWEEN_JAPANESE.sub("", joined)
    joined = _SPACE_BEFORE_PUNCTUATION.sub("", joined)
    joined = _OCR_JUNK.sub("", joined)
    # "ふり。・" -> "ふり。": the arrow after the line is not part of it.
    joined = _NOISE_BEFORE_PUNCTUATION.sub("", joined)
    joined = _TRAILING_NOISE.sub("", joined)
    # Tesseract sometimes reads a digit as its circled form ("⑤" for "5").
    joined = joined.translate(_CIRCLED_DIGITS)
    return joined.strip()


def _isolate_letters(image: Image.Image) -> Image.Image:
    """Throw the scene away and leave the dialogue letters.

    Game dialogue is white text with a dark outline, drawn over whatever the
    scene happens to be. A capture box that reaches a little above or below
    the line therefore also contains background - and Tesseract will happily
    read desks and chairs as kanji, which then get translated into nonsense.
    Sharpening first makes the letters stand out from the smooth background,
    then "keep only the brightest pixels" erases everything that is not text.
    """
    grey = image.convert("L")
    sharp = grey.filter(ImageFilter.UnsharpMask(radius=3, percent=250, threshold=0))
    return sharp.point(lambda value: 255 if value >= 225 else 0)


_LETTER_RE = re.compile(r"[^\W\d_]", re.UNICODE)     # any letter, any script
_KANA_RE = re.compile(r"[\u3040-\u30ff\uff66-\uff9f]")    # hiragana + katakana

# The language group that last produced a good read. Trying it first keeps the
# common case at one OCR pass instead of ten.
_LAST_LANGUAGE = None


def _ocr(prepared: Image.Image, language: str) -> tuple:
    """Run Tesseract on an image. Returns (text, how sure it is, 0-100).

    psm 6 = treat the picture as one block of text. image_to_data is used
    instead of image_to_string because it also reports a confidence per word,
    which is how we tell a real read from Dutch being read as Korean.
    """
    # Game text is small, so it is enlarged: that is the biggest quality win.
    prepared = prepared.resize(
        (prepared.width * 2, prepared.height * 2), Image.LANCZOS
    )
    data = pytesseract.image_to_data(
        prepared, lang=language, config="--psm 6",
        output_type=pytesseract.Output.DICT,
    )

    lines = {}
    scores = []
    for index, word in enumerate(data["text"]):
        word = word.strip()
        if not word:
            continue
        key = (data["block_num"][index], data["par_num"][index], data["line_num"][index])
        lines.setdefault(key, []).append(word)
        try:
            score = float(data["conf"][index])
        except (TypeError, ValueError):
            continue
        if score >= 0:
            scores.append(score)

    text = "\n".join(" ".join(words) for _key, words in sorted(lines.items()))
    confidence = sum(scores) / len(scores) if scores else 0.0
    return text, confidence


def _is_hit(text: str, confidence: float, script: str) -> bool:
    """Is this a read we can trust, in the script we were expecting?

    Two things must agree: Tesseract must be sure of the words, and the script
    we were expecting must be a real part of the answer - not one stray
    character. Without the second rule, French read as "Russian" picks up a
    single Cyrillic letter and wins on confidence alone.
    """
    if confidence < MIN_CONFIDENCE or len(text) < 3:
        return False
    expected = len(re.findall(script, text))
    letters = len(_LETTER_RE.findall(text))
    if expected < 2 or letters < 2:
        return False
    return expected * 10 >= letters * 3      # at least 30% in that script


def _japanese_evidence(text: str) -> bool:
    """Is there real kana in this read? Kanji alone could be Chinese."""
    kana = len(_KANA_RE.findall(text))
    return kana >= 2 and kana * 10 >= len(text)


def _read_best(image: Image.Image, first=None, only=None) -> tuple:
    """Read one image with the language packs, cheapest and likeliest first.

    Returns (text, confidence, language or None). A pack only counts when it is
    sure of the words and the script it expected is really there, so a wrong
    pack scoring 40 still loses to the right one. Kanji with no kana is kept as
    a candidate instead of an answer, because that text may be Chinese - the
    Chinese packs get a chance to beat it on confidence.
    """
    scripts = dict(LANGUAGE_GROUPS)

    def usable(language: str) -> bool:
        return (
            language in scripts
            and _language_available(language)
            and (only is None or language in only)
        )

    best_any = ("", -1.0)
    best_hit = None

    def consider(language: str, cleaned: str, confidence: float):
        """Book-keep one pack's answer; return it when it settles the question."""
        nonlocal best_any, best_hit
        if not cleaned:
            return None
        if confidence > best_any[1]:
            best_any = (cleaned, confidence)
        if not _is_hit(cleaned, confidence, scripts[language]):
            return None

        if language == "jpn+eng":
            if _japanese_evidence(cleaned):
                return (cleaned, confidence, language)      # unmistakably Japanese
            if best_hit is None or confidence > best_hit[1]:
                best_hit = (cleaned, confidence, language)  # maybe Chinese
            return None                                    # let the Chinese packs try

        if best_hit is not None and best_hit[1] > confidence:
            # A kanji-only Japanese read was more believable than this one.
            # Only the Chinese packs could beat it, so stop after them.
            return best_hit if language.startswith("chi") else None
        return (cleaned, confidence, language)

    # Two quick packs first: the last known language and English. Between them
    # they cover Japanese and every Latin-script language in about a second.
    quick = [first] if first else ["jpn+eng"]
    if "eng" not in quick:
        quick.append("eng")
    quick = [language for language in quick if usable(language)]

    readings = 0
    empty = 0
    for language in quick:
        raw, confidence = _ocr(image, language)
        cleaned = clean_ocr_text(raw)
        readings += 1
        if not cleaned:
            empty += 1
        answer = consider(language, cleaned, confidence)
        if answer:
            return answer

    # Empty box, or a menu with no dialogue: both quick packs saw nothing at
    # all, so do not spend ten more OCR passes looking for text that is not
    # there (that is what made live mode freeze on such screens).
    if readings >= 2 and empty == readings:
        return "", 0.0, None

    for language, _script in LANGUAGE_GROUPS:
        if language in quick or not usable(language):
            continue
        raw, confidence = _ocr(image, language)
        answer = consider(language, clean_ocr_text(raw), confidence)
        if answer:
            return answer

    if best_hit:
        return best_hit
    return best_any[0], best_any[1], None


def read_text(image: Image.Image) -> str:
    """Read the dialogue in an image, in whatever language it is written."""
    global _LAST_LANGUAGE
    _configure_tesseract()

    text, confidence, language = _read_best(_isolate_letters(image), first=_LAST_LANGUAGE)
    if language:
        _LAST_LANGUAGE = language
        return text

    # The bright-pixel filter removes grey or coloured text, so give the plain
    # picture one more chance with the languages that are likely to be right.
    plain, plain_confidence, plain_language = _read_best(
        image.convert("L"),
        first=_LAST_LANGUAGE or "jpn+eng",
        only={_LAST_LANGUAGE or "jpn+eng", "eng"},
    )
    if plain_language:
        _LAST_LANGUAGE = plain_language
        return plain
    return plain if plain_confidence > confidence else text


def ocr_region() -> str:
    """Screenshot the fixed region and return the text that is in it."""
    _configure_tesseract()
    return read_text(capture_region())
