"""
Prompt-difficulty classifier for the `auto` routing alias.

The spec explicitly requires this to beat a length-only baseline, with the
eval set containing "short-but-hard" and "long-but-trivial" traps. A pure
length heuristic fails on both of those by construction, so we score on a
small set of *signals* instead of raw character count:

  - reasoning/analysis verbs & connectives ("prove", "why", "compare",
    "optimize", "step by step", "trade-off") -> push toward "smart"
  - code / math / multi-constraint structure (code fences, equations,
    numbered constraints, "and" chains) -> push toward "smart"
  - simple lookup / greeting / single-fact patterns -> push toward "fast"
  - raw length contributes only a small, capped weight, specifically so a
    long-but-trivial prompt (e.g. a long list of items to just repeat back)
    doesn't get pushed to "smart" on length alone, and a short-but-hard
    prompt ("Prove sqrt(2) is irrational.") isn't kept on "fast".

Returns a difficulty score in [0, 1]; the caller compares against a
threshold (default 0.5, configurable in gateway_config).
"""
import re

HARD_SIGNALS = [
    r"\bprove\b", r"\bwhy\b", r"\bexplain\b.*\b(why|how)\b", r"\bcompare\b",
    r"\boptimi[sz]e\b", r"\bdesign\b", r"\barchitecture\b", r"\btrade-?off",
    r"\bderive\b", r"\bproof\b", r"\bdebug\b", r"\brefactor\b",
    r"\bstep by step\b", r"\balgorithm\b", r"\bedge cases?\b",
    r"\bwrite (a|an) (function|program|script|class)\b",
    r"```", r"\b\d+\s*[\+\-\*/\^]\s*\d+\b", r"\bintegral\b", r"\bderivative\b",
    r"\bo\(n", r"\bcomplexity\b", r"\bconcurrency\b", r"\brace condition\b",
    r"\bnp-hard\b", r"\brecursion\b", r"\bcounter-?example\b",
]

EASY_SIGNALS = [
    r"^(hi|hello|hey)\b", r"\bwhat (is|are) the capital\b",
    r"\bwhat time\b", r"\bthank(s| you)\b", r"\bwhat('s| is) \d",
    r"\bdefine\b", r"\bspell\b", r"\btranslate\b",
    r"^(yes|no|ok|okay)\b", r"\brepeat (this|the following)\b",
    r"\blist the following\b", r"\bcopy\b",
]


def score_difficulty(prompt: str) -> float:
    text = prompt.strip().lower()
    hard_hits = sum(1 for pat in HARD_SIGNALS if re.search(pat, text))
    easy_hits = sum(1 for pat in EASY_SIGNALS if re.search(pat, text))

    signal_score = 0.5 + 0.15 * hard_hits - 0.2 * easy_hits

    # Small, capped length contribution (length alone must never dominate).
    word_count = len(text.split())
    length_score = min(word_count / 400.0, 0.15)  # capped at +0.15

    # Multi-constraint prompts (several "and"/numbered clauses) skew harder,
    # independent of raw length -- this is what defeats "long-but-trivial".
    clause_bonus = 0.0
    if len(re.findall(r"\band\b", text)) >= 3 or re.search(r"\b\d\)\s", text):
        clause_bonus = 0.1

    score = signal_score + length_score + clause_bonus
    return max(0.0, min(1.0, score))


def classify(prompt: str, threshold: float) -> str:
    return "smart" if score_difficulty(prompt) >= threshold else "fast"
