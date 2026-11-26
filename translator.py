"""
translator.py - the ONLY file in this app that talks to the internet.

The rest of the app calls exactly one function:

    translate(text) -> translated text

If a translation service fails, it retries a couple of times and then raises
TranslationError. main.py catches that, keeps the last translation on screen
and shows a small warning instead of crashing.

TODO - how to swap the translation service later
================================================
You only ever edit this file.

A) Google Cloud Translation (official, needs billing enabled)
   1. Create a .env file in this same folder containing one line:
          GOOGLE_TRANSLATE_API_KEY=your-key-here
   2. Restart the app. That is all - _api_key() below picks it up and
      translate() switches to the official API automatically.
   3. Pricing: ~$20 per million characters, and the first 500k chars/month
      are usually free. Keyless mode (the three free endpoints tried by
      _translate_keyless) is used whenever .env is missing or empty.

B) Naver Papago (same result, needs Client ID + Secret)
   1. Add to .env:  PAPAGO_CLIENT_ID=...   PAPAGO_CLIENT_SECRET=...
   2. Write _translate_papago(text) that POSTs to
          https://openapi.naver.com/v1/papago/n2mt
      with headers X-Naver-Client-Id / X-Naver-Client-Secret and
      data {"source": "auto", "target": TARGET_LANG, "text": text}.
      (Use the detected code instead of "auto" if Papago refuses it.)
   3. In translate(), branch to it when PAPAGO_CLIENT_ID is set.
   4. Papago accepts at most 10,000 characters per request.

C) Any other service: keep the same function name and signature and make it
   return a plain string. Nothing outside this file needs to change.

D) The game's language is not Japanese any more
   Nothing here has to change: the services are asked to detect the language
   themselves (SOURCE_LANG = "auto"). Only the OCR side needs the matching
   Tesseract pack, which ensure_ocr_languages() downloads at startup.
"""

import html
import os
import re
import time

import requests

# ---------------------------------------------------------------- settings --
SOURCE_LANG = "auto"  # "auto" = the service detects the game's language
TARGET_LANG = "en"   # the language you want to read on screen
TIMEOUT_SECONDS = 6
RETRIES = 1               # extra attempts after the first failure
RETRY_DELAY_SECONDS = 1.0

# A dead endpoint is skipped for a while instead of costing a round trip on
# every single translation. Blocked/rate-limited: a few minutes (a 429 usually
# clears quickly, and waiting longer pushes everything onto the worst service).
BLOCKED_COOLDOWN_SECONDS = 180
TIMEOUT_COOLDOWN_SECONDS = 60

# Three free, unofficial, no-key endpoints, tried in this order. They are not
# a promise: any of them can rate-limit (HTTP 429) or change without notice.
KEYLESS_URL = "https://clients5.google.com/translate_a/t"
GTX_URL = "https://translate.googleapis.com/translate_a/single"
MYMEMORY_URL = "https://api.mymemory.translated.net/get"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    )
}

# One session for the whole program: keeps the connection open, which saves a
# TLS handshake on every translation (~0.2 s each).
SESSION = requests.Session()
SESSION.headers.update(HEADERS)

# endpoint name -> time.monotonic() before which we do not call it again
_SKIP_UNTIL = {}

# Official Google Cloud Translation (v2), used only when a key exists.
GOOGLE_V2_URL = "https://translation.googleapis.com/language/translate/v2"

ENV_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")

# ------------------------------------------------------------ OCR languages --
# Tesseract needs one small data file per script it reads. They are not in the
# repository; each one is fetched once, into the tessdata folder, so the OCR can
# read a game written in any of them. This is the only other place in the app
# that talks to the internet, and it is not part of translating.
OCR_TESSDATA_URL = (
    "https://raw.githubusercontent.com/tesseract-ocr/tessdata_fast/main"
    "/{code}.traineddata"
)
OCR_LANGUAGES = (
    "jpn", "eng",                       # Japanese and English: the common case
    "chi_sim", "chi_tra", "kor", "rus", "ara", "tha", "heb", "ell", "hin",
)
OCR_DOWNLOAD_TIMEOUT = 60


def ensure_ocr_languages(directory: str, codes=OCR_LANGUAGES) -> list:
    """Download any missing Tesseract language packs. Best effort, never raises.

    Each file is written under a .part name and renamed at the end, so a
    half-finished download can never be used as language data.
    """
    try:
        os.makedirs(directory, exist_ok=True)
    except OSError as exc:
        print(f"OCR: could not create {directory}: {exc}", flush=True)
        return []

    added = []
    for code in codes:
        target = os.path.join(directory, f"{code}.traineddata")
        if os.path.exists(target):
            continue
        partial = target + ".part"
        try:
            response = SESSION.get(
                OCR_TESSDATA_URL.format(code=code),
                timeout=OCR_DOWNLOAD_TIMEOUT,
                stream=True,
            )
            response.raise_for_status()
            with open(partial, "wb") as handle:
                for chunk in response.iter_content(65536):
                    handle.write(chunk)
            os.replace(partial, target)
        except Exception as exc:              # offline, GitHub down, disk full...
            try:
                os.remove(partial)
            except OSError:
                pass
            print(f"OCR: could not download {code} ({exc})", flush=True)
            continue
        added.append(code)
        print(f"OCR: added {code} language data", flush=True)
    return added


class TranslationError(Exception):
    """The translation service could not be reached or refused the request."""


def _read_api_key() -> str:
    """Return the Google key from the environment or .env, or "" if there is none."""
    key = os.environ.get("GOOGLE_TRANSLATE_API_KEY", "").strip()
    if key:
        return key

    # A missing or unreadable .env is not an error - we just stay keyless.
    if not os.path.isfile(ENV_FILE):
        return ""
    try:
        with open(ENV_FILE, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                name, value = line.split("=", 1)
                if name.strip() == "GOOGLE_TRANSLATE_API_KEY":
                    return value.strip().strip('"').strip("'")
    except OSError:
        return ""
    return ""


# Any letter in any script. Used to tell real text from stray marks.
_LETTER_RE = re.compile(r"[^\W\d_]", re.UNICODE)
_LATIN_RE = re.compile(r"[A-Za-z]")

# Letters that are not Latin: Japanese, Chinese, Korean, Cyrillic, Arabic,
# Thai, Hebrew, Greek, Hindi... A good English answer keeps only a few of them
# (a name, usually), so a high share means the source came back unchanged.
_NON_LATIN_RE = re.compile(
    r"[\u0370-\u03ff\u0400-\u052f\u0590-\u06ff\u0900-\u0dff"
    r"\u0e00-\u0e7f\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff"
    r"\uac00-\ud7af\uff66-\uff9f]"
)


# A free service asked to "translate" English to English answers with a
# complaint instead of a translation. That is not text to show on screen.
_SERVICE_COMPLAINTS = (
    "select two distinct languages",
    "invalid language pair",
    "same language",
    "query length limit",
    "mymemory warning",
    "invalid source language",
)


def _is_complaint(text: str) -> bool:
    lowered = (text or "").lower()
    return any(marker in lowered for marker in _SERVICE_COMPLAINTS)


def _is_usable(source: str, translated: str) -> bool:
    """Is this answer good enough to put on the screen?

    A free endpoint asked to translate half a sentence sometimes answers with
    just "." or hands the original back unchanged. That is exactly the "random
    words" look, so it counts as a failure and the next endpoint gets a turn.
    """
    text = (translated or "").strip()
    if _is_complaint(text):
        return False
    if len(_LATIN_RE.findall(text)) < 2:
        return False                     # no English anywhere in it
    if len(text) < 2:
        return False
    # Normally nothing of the input survives; a name kept in kanji is fine.
    non_latin = len(_NON_LATIN_RE.findall(text))
    return non_latin * 5 <= len(text) * 2     # not more than 40% still foreign


def _join_segments(pieces) -> str:
    """Glue the service's sentence pieces together with the right spacing.

    Google splits a translation into pieces at sentence boundaries. Most
    pieces already end with a space, but not all of them do, and joining those
    blindly gives "...the hill.There must be someone." or, worse,
    "...thelightisstillburning". So: add a space only where two words would
    otherwise touch.
    """
    text = ""
    for piece in pieces:
        if not piece:
            continue
        if (
            text
            and not text.endswith((" ", "\n"))
            and not piece.startswith((" ", "\n"))
            and text[-1].isalnum()
            and piece[0].isalnum()
        ):
            text += " "
        text += piece
    return re.sub(r"[ \t]+", " ", text).strip()


def tidy_translation(text: str) -> str:
    """Small repairs to what the service sends back, before it hits the screen."""
    text = re.sub(r"[ \t]+", " ", html.unescape(text or "")).strip()
    text = re.sub(r"[ \t]+([,.!?;:)\]}])", r"\1", text)   # "hello ," -> "hello,"
    text = re.sub(r"([,;:])(?=[A-Za-z])", r"\1 ", text)   # "a,b" -> "a, b"
    # "end.Next" / "end.next" -> add the missing space after a sentence stop.
    # The uppercase-before guard leaves initials alone ("U.S.A."), and digits
    # are excluded, so decimals like "3.5" are untouched.
    text = re.sub(r"(?<![A-Z0-9])([.!?])(?=[A-Za-z])", r"\1 ", text)
    text = text.strip()
    if text[:1].islower():                                  # "yes." -> "Yes."
        text = text[0].upper() + text[1:]
    return text


def _translate_dict_chrome(text: str) -> str:
    """Free endpoint #1: what the Chrome dictionary uses. Usually the best one."""
    response = SESSION.get(
        KEYLESS_URL,
        params={"client": "dict-chrome-ex", "sl": SOURCE_LANG, "tl": TARGET_LANG, "q": text},
        timeout=TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    data = response.json()
    # Shape: ["translated sentence"] (sometimes one string per sentence).
    pieces = [piece for piece in data if isinstance(piece, str)] if isinstance(data, list) else []
    translated = _join_segments(pieces)
    if not translated:
        raise TranslationError("the service returned empty text")
    return translated


def _translate_gtx(text: str) -> str:
    """Free endpoint #2: the classic unofficial Google Translate endpoint."""
    response = SESSION.get(
        GTX_URL,
        params={"client": "gtx", "sl": SOURCE_LANG, "tl": TARGET_LANG, "dt": "t", "q": text},
        timeout=TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    data = response.json()
    # Shape: [[["translated", "original", ...], ...], ...]
    pieces = data[0] or []
    translated = _join_segments(piece[0] for piece in pieces if piece and piece[0])
    if not translated:
        raise TranslationError("the service returned empty text")
    return translated


def _translate_mymemory(text: str) -> str:
    """Free endpoint #3: MyMemory, as a last resort when Google rate-limits us."""
    response = SESSION.get(
        MYMEMORY_URL,
        # MyMemory needs a source language; "Autodetect" is its name for auto.
        params={"q": text, "langpair": f"Autodetect|{TARGET_LANG}"},
        timeout=TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    data = response.json()
    translated = tidy_translation(data["responseData"]["translatedText"])
    if not translated:
        raise TranslationError("the service returned empty text")
    return translated


def _cooldown_for(exc: Exception) -> int:
    """How long to leave a failing endpoint alone (0 = just try it next time)."""
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status in (403, 429):
        return BLOCKED_COOLDOWN_SECONDS      # blocked or rate-limited
    if isinstance(exc, (requests.Timeout, requests.ConnectionError)):
        return TIMEOUT_COOLDOWN_SECONDS
    return 0


def _translate_keyless(text: str) -> str:
    """Try each free endpoint in turn; only fail when all of them failed."""
    endpoints = (
        ("dict-chrome", _translate_dict_chrome),
        ("gtx", _translate_gtx),
        ("mymemory", _translate_mymemory),
    )

    now = time.monotonic()
    usable = [item for item in endpoints if _SKIP_UNTIL.get(item[0], 0) <= now]
    if not usable:
        # Everything is cooling down; better to try them than to refuse.
        usable = list(endpoints)

    last_error = None
    already_english = False
    for name, endpoint in usable:
        try:
            translated = endpoint(text)
        except Exception as exc:
            last_error = exc
            pause = _cooldown_for(exc)
            if pause:
                _SKIP_UNTIL[name] = time.monotonic() + pause
            continue
        if _is_complaint(translated):
            # The service says this is already English: show it unchanged
            # instead of its complaint.
            already_english = True
            last_error = f"{name} reported nothing to translate"
            continue
        if _is_usable(text, translated):
            return translated
        # The service answered, but with nothing worth showing ("", ".", or
        # the original unchanged). Try the next one instead of drawing it.
        last_error = f"{name} returned no usable English: {translated!r}"
    if already_english:
        return text
    raise TranslationError(f"all free endpoints failed: {last_error}")


def _translate_google(text: str, api_key: str) -> str:
    """The official API, used when GOOGLE_TRANSLATE_API_KEY is available."""
    payload = {"q": text, "target": TARGET_LANG, "format": "text"}
    if SOURCE_LANG != "auto":
        payload["source"] = SOURCE_LANG        # omitted = Google detects it
    response = requests.post(
        GOOGLE_V2_URL,
        params={"key": api_key},
        json=payload,
        timeout=TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    translated = response.json()["data"]["translations"][0]["translatedText"]
    return tidy_translation(translated)


def translate(text: str) -> str:
    """Any language -> English. Retries a few times, then raises TranslationError."""
    text = (text or "").strip()
    if not text:
        return ""

    api_key = _read_api_key()
    last_error = None

    for attempt in range(RETRIES + 1):
        try:
            if api_key:
                return tidy_translation(_translate_google(text, api_key))
            return tidy_translation(_translate_keyless(text))
        except Exception as exc:                      # network, HTTP 429, bad JSON...
            last_error = exc
            if attempt < RETRIES:
                time.sleep(RETRY_DELAY_SECONDS)

    raise TranslationError(str(last_error) or "unknown error")
