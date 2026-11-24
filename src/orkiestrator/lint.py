"""Brand-voice lint: runs on every submit so nothing reaches the owner with machine fingerprints."""
import re

BANNED = ("delve", "unlock", "game-changer", "game changer", "in today's fast-paced world", "let's dive in", "dive in")
INVISIBLE = re.compile("[​‌‍⁠﻿]")


def check(text):
    """Returns a list of problems; empty means clean."""
    problems = []
    if "—" in text or "–" in text:
        problems.append("contains an em/en dash")
    if INVISIBLE.search(text):
        problems.append("contains invisible characters")
    low = text.lower()
    problems += [f"banned phrase: {w!r}" for w in BANNED if w in low]
    return problems
