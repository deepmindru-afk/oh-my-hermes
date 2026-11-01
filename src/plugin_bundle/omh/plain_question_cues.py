"""When a message is a plain knowledge question: one short sentence, no work object.

"Explain TCP vs UDP briefly" or "클로저가 뭐야?" asks the model to answer from
what it knows. The skill catalog's words ("explain", "review", "merge") also
appear in such questions, so a token-only match would hang a workflow on a
message that has no work in it. This predicate is the one place that says
"plain question"; the per-turn candidate line (`skill_shortlist`) and the route
hint's token-only rules (`awareness`) both stand down when it holds.

It fails closed. A message is plain only when all of these hold:

- one line, one sentence, at most 20 words (English) or 40 characters (Hangul);
- no URL, file path, code fence, backtick, or `#<number>`;
- it opens like a question or an explain request (English), or carries a
  question or explain cue (Hangul);
- it names no work object: a deictic word (this, these, that, my, our,
  attached; 이, 저, 첨부, 우리) followed within three words by a work noun
  (paper, pdf, file, code, repo, pr, log, trace, error, project, ...), or, in
  Hangul, any work noun at all.

Only words are read, on a message already capped in length. Stdlib only.
"""

from __future__ import annotations

import re

_MAX_WORDS = 20
_MAX_HANGUL_CHARS = 40

_HANGUL_RE = re.compile(r"[\uac00-\ud7a3]")
_WORD_RE = re.compile(r"[a-z0-9]+(?:'[a-z]+)?")
_SENTENCE_BREAK_RE = re.compile(r"[.!?]\s+\S")
# A URL, a file path (a slash between word characters, a dotted file name with
# a known extension, a home or relative prefix), a code fence or backtick, or
# an issue / PR number.
_WORK_SHAPE_RE = re.compile(
    r"https?://|www\.|`|#\d"
    r"|(?:^|\s)(?:~|\.{1,2})?/\S"
    r"|\w/\w"
    r"|\b\w+\.(?:py|js|ts|tsx|jsx|json|ya?ml|toml|md|txt|csv|pdf|rs|go|java|sh|c|cpp|h|rb|sql)\b",
    re.IGNORECASE,
)

_LEAD_FILLERS = (
    "quick question:",
    "quick question",
    "hey,",
    "hey",
    "hi,",
    "hi",
    "hermes,",
    "please",
    "pls",
    "can you",
    "could you",
    "would you",
    "will you",
)
_QUESTION_OPENERS = frozenset(
    {
        "explain",
        "what",
        "what's",
        "whats",
        "how",
        "why",
        "who",
        "when",
        "where",
        "which",
        "is",
        "are",
        "does",
        "do",
        "did",
        "can",
        "should",
        "best",
        "difference",
        "define",
        "describe",
        "tell",
        "meaning",
    }
)
_DEICTICS = frozenset({"this", "these", "that", "those", "my", "our", "attached", "your", "its", "their"})
_WORK_NOUNS = frozenset(
    {
        "paper", "papers", "pdf", "pdfs", "file", "files", "code", "codebase", "repo", "repos", "repository",
        "pr", "prs", "log", "logs", "trace", "traceback", "error", "errors", "project", "projects",
        "ci", "build", "pipeline", "server", "service", "app", "bug", "issue", "function", "script",
        "query", "config", "test", "tests", "branch", "commit", "diff", "deck", "doc", "docs",
        "document", "dataset", "data", "spreadsheet", "screenshot", "image", "email", "ticket",
        "arxiv", "stack", "crash", "output", "report", "database", "schema", "module", "class",
    }
)
_DEICTIC_WINDOW = 3

_HANGUL_QUESTION_CUES = (
    "뭐", "무엇", "뭔", "차이", "설명", "알려", "방법", "어디", "왜", "어떻게", "언제", "누구", "어떤", "?",
)
_HANGUL_WORK_NOUNS = (
    "논문", "파일", "코드", "레포", "리포", "로그", "에러", "오류", "프로젝트", "첨부", "스택",
    "서버", "빌드", "배포",
)
_HANGUL_PR_RE = re.compile(r"(?<![a-z])pr(?![a-z])", re.IGNORECASE)
_HANGUL_DEICTIC_RE = re.compile(r"(?:^|\s)(?:이|저|그|우리|내|제)\s")


def plain_question(message: str) -> bool:
    """True for a one-sentence knowledge question with no work object."""
    text = message.strip()
    if not text or "\n" in text or "\r" in text or len(text) > 200:
        return False
    if _WORK_SHAPE_RE.search(text) or _SENTENCE_BREAK_RE.search(text):
        return False
    if _HANGUL_RE.search(text):
        return _plain_hangul(text)
    return _plain_english(text)


def _plain_hangul(text: str) -> bool:
    if len(text) > _MAX_HANGUL_CHARS:
        return False
    if any(noun in text for noun in _HANGUL_WORK_NOUNS) or _HANGUL_PR_RE.search(text):
        return False
    if _HANGUL_DEICTIC_RE.search(text):
        return False
    return any(cue in text for cue in _HANGUL_QUESTION_CUES)


def _plain_english(text: str) -> bool:
    lowered = text.lower().replace("\u2019", "'")
    if not lowered.isascii():
        return False
    stripped = lowered
    for _ in range(2):
        for filler in _LEAD_FILLERS:
            if stripped.startswith(filler) and not stripped[len(filler) : len(filler) + 1].isalnum():
                stripped = stripped[len(filler) :].lstrip(" ,:")
                break
    words = _WORD_RE.findall(stripped)
    if not words or len(words) > _MAX_WORDS or words[0] not in _QUESTION_OPENERS:
        return False
    for index, word in enumerate(words):
        if word in _DEICTICS and any(
            following in _WORK_NOUNS for following in words[index + 1 : index + 1 + _DEICTIC_WINDOW]
        ):
            return False
    return True
