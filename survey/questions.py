from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class ParserType(str, Enum):
    KEYWORD = "keyword"   # yes/no and simple binary questions
    FUZZY   = "fuzzy"     # multi-option closed questions, best-match scoring
    REGEX   = "regex"     # extract a number (Q2A: bags/acres)
    LLM     = "llm"       # open-ended short answer — Gemini extracts entity in-call
    RAW     = "raw"       # store verbatim, LLM processes post-call (Q8)


@dataclass
class Option:
    key: str           # routing key used in ROUTING table
    text: str          # full spoken option text
    keywords: list[str] = field(default_factory=list)


@dataclass
class Question:
    id: str
    text: str          # English question text
    audio_file: str    # pre-recorded MP3 in audios/
    parser_type: ParserType
    options: list[Option] = field(default_factory=list)
    allows_reprompt: bool = False   # play SYS_003 and re-listen once on unclear
    reprompt_audio: str = "SYS_003.wav"
    # where to route after two consecutive unclear responses
    unclear_fallback: Optional[str] = None


# ---------------------------------------------------------------------------
# Question graph — mirrors routing-logic.mermaid exactly
# ---------------------------------------------------------------------------

QUESTIONS: dict[str, Question] = {

    "OPEN": Question(
        id="OPEN",
        text="Hello, this is a call from AgriCo. Is now a good time?",
        audio_file="SYS_001.wav",
        parser_type=ParserType.KEYWORD,
        options=[
            Option("yes", "Yes", ["yes", "yeah", "yep", "okay", "ok", "sure", "fine",
                                  "go ahead", "please", "i'm ready", "ready"]),
            Option("no",  "No",  ["no", "busy", "not now", "later", "bad time",
                                  "call back", "cannot", "can't", "not ready"]),
        ],
        allows_reprompt=True,
        unclear_fallback="CALLBACK",
    ),

    "Q1": Question(
        id="Q1",
        text="Did you buy SeedX maize seed after the field day?",
        audio_file="Q1.wav",
        parser_type=ParserType.KEYWORD,
        options=[
            Option("yes", "Yes", ["yes", "yeah", "bought", "purchased", "got it",
                                  "i did", "i have", "already", "did buy"]),
            Option("no",  "No",  ["no", "didn't", "did not", "haven't", "not yet",
                                  "no i", "i did not"]),
        ],
        allows_reprompt=True,
        unclear_fallback="Q3",   # treat unclear as "no" after re-prompt
    ),

    "Q2": Question(
        id="Q2",
        text="Have you planted it yet?",
        audio_file="Q2.wav",
        parser_type=ParserType.FUZZY,
        options=[
            Option("planted",  "Yes, already planted",
                   ["already", "planted", "yes planted", "i planted", "done",
                    "in the ground", "have planted"]),
            Option("planning", "Not yet, planning to plant this season",
                   ["planning", "plan", "this season", "soon", "going to",
                    "will plant", "intend", "about to"]),
            Option("waiting",  "Not yet, waiting for next season",
                   ["next season", "next year", "waiting", "not this season",
                    "later season", "hold"]),
        ],
        allows_reprompt=False,
    ),

    "Q2A": Question(
        id="Q2A",
        text="How many bags or acres did you plant with SeedX?",
        audio_file="Q2A.wav",
        parser_type=ParserType.REGEX,
        allows_reprompt=False,
    ),

    "Q3": Question(
        id="Q3",
        text="What was the main reason you didn't buy SeedX? "
             "Was it too expensive, couldn't find it near you, "
             "not ready to try a new seed, bought a different seed, or something else?",
        audio_file="Q3.wav",
        parser_type=ParserType.FUZZY,
        options=[
            Option("expensive",      "Too expensive",
                   ["expensive", "costly", "price", "cost", "money", "afford",
                    "too much", "dear", "pricey"]),
            Option("not_available",  "Couldn't find it near me / not available",
                   ["find", "available", "near", "location", "not there",
                    "far", "stock", "couldn't get", "nowhere"]),
            Option("not_ready",      "Not ready to try a new seed yet",
                   ["new", "ready", "try", "first time", "unknown", "risk",
                    "familiar", "trust", "never used", "scared"]),
            Option("different_seed", "Bought a different seed instead",
                   ["different", "other", "another", "bought", "instead",
                    "chose", "other seed"]),
            Option("something_else", "Something else",
                   ["else", "other reason", "different reason", "personal",
                    "no reason"]),
        ],
        allows_reprompt=False,
    ),

    "Q3A": Question(
        id="Q3A",
        text="Which seed did you buy?",
        audio_file="Q3A.wav",
        parser_type=ParserType.LLM,
        allows_reprompt=False,
    ),

    "Q4": Question(
        id="Q4",
        text="Since the field day, have you bought fertilizer for this season?",
        audio_file="Q4.wav",
        parser_type=ParserType.KEYWORD,
        options=[
            Option("yes", "Yes", ["yes", "yeah", "bought", "purchased", "got",
                                  "i did", "i have", "already bought", "got fertilizer"]),
            Option("no",  "No",  ["no", "didn't", "did not", "haven't", "not yet",
                                  "not bought", "no fertilizer"]),
        ],
        allows_reprompt=True,
        unclear_fallback="Q7",   # treat unclear as "no"
    ),

    "Q5": Question(
        id="Q5",
        text="What fertilizer did you buy?",
        audio_file="Q5.wav",
        parser_type=ParserType.LLM,
        allows_reprompt=True,
    ),

    "Q6": Question(
        id="Q6",
        text="Where did you buy it from? Which agro shop or dealer?",
        audio_file="Q6.wav",
        parser_type=ParserType.LLM,
        allows_reprompt=True,
    ),

    "Q7": Question(
        id="Q7",
        text="After attending the field day, did you tell any other farmers about AgriCo or SeedX?",
        audio_file="Q7.wav",
        parser_type=ParserType.KEYWORD,
        options=[
            Option("yes", "Yes", ["yes", "yeah", "told", "shared", "spread",
                                  "talked", "mentioned", "spoke", "informed",
                                  "advised", "recommended"]),
            Option("no",  "No",  ["no", "didn't", "did not", "haven't",
                                  "no one", "nobody"]),
        ],
        allows_reprompt=False,
    ),

    "Q8": Question(
        id="Q8",
        text="Is there anything from the field day that you remember most — "
             "something that really stuck with you?",
        audio_file="Q8.wav",
        parser_type=ParserType.RAW,   # store raw; LLM codes themes post-call
        allows_reprompt=False,
    ),

    "Q9": Question(
        id="Q9",
        text="Are you planning to use any AgriCo products next season?",
        audio_file="Q9.wav",
        parser_type=ParserType.FUZZY,
        options=[
            Option("definitely_yes", "Yes, definitely",
                   ["definitely", "absolutely", "for sure", "certainly",
                    "yes definitely", "100"]),
            Option("probably_yes",   "Probably yes",
                   ["probably", "likely", "think so", "should", "maybe yes",
                    "most likely"]),
            Option("not_sure",       "Not sure",
                   ["not sure", "maybe", "unsure", "don't know", "perhaps",
                    "can't say", "undecided"]),
            Option("probably_not",   "Probably not",
                   ["probably not", "unlikely", "doubt", "not really",
                    "don't think so"]),
            Option("no",             "No",
                   ["no", "definitely not", "won't", "will not", "never",
                    "not planning"]),
        ],
        allows_reprompt=False,
    ),

    "Q10": Question(
        id="Q10",
        text="What would most help you as a farmer right now — "
             "better access to inputs, better prices, or more knowledge about how to farm better?",
        audio_file="Q10.wav",
        parser_type=ParserType.FUZZY,
        options=[
            Option("access",    "Better access to inputs",
                   ["access", "inputs", "find", "availability", "near",
                    "supply", "get", "obtain"]),
            Option("price",     "Better prices",
                   ["price", "prices", "cheaper", "cost", "afford",
                    "money", "reduce", "lower"]),
            Option("knowledge", "More knowledge about how to farm better",
                   ["knowledge", "training", "learn", "how to", "information",
                    "education", "farm better", "advice", "teach"]),
        ],
        allows_reprompt=False,
    ),
}

# ---------------------------------------------------------------------------
# Routing table — answer_key → next question ID
# "*" is a wildcard (any answer routes the same way)
# "TIME_CHECK" is resolved dynamically by the session state machine
# ---------------------------------------------------------------------------

ROUTING: dict[str, dict[str, str]] = {
    "OPEN": {
        "yes":      "Q1",
        "no":       "CALLBACK",
        "unclear":  "CALLBACK",
    },
    "Q1": {
        "yes":     "Q2",
        "no":      "Q3",
        "unclear": "Q3",
    },
    "Q2": {
        "planted":  "Q2A",
        "planning": "Q4",
        "waiting":  "Q4",
        "unclear":  "Q4",
    },
    "Q2A": {"*": "Q4"},
    "Q3": {
        "expensive":      "Q4",
        "not_available":  "Q4",
        "not_ready":      "Q4",
        "different_seed": "Q3A",
        "something_else": "Q4",
        "unclear":        "Q4",
    },
    "Q3A": {"*": "Q4"},
    "Q4": {
        "yes":     "Q5",
        "no":      "Q7",
        "unclear": "Q7",
    },
    "Q5": {"*": "Q6"},
    "Q6": {"*": "Q7"},
    "Q7": {
        "yes":     "Q8",
        "no":      "Q8",
        "unclear": "Q8",
    },
    "Q8":  {"*": "Q9"},
    "Q9":  {"*": "TIME_CHECK"},  # resolved dynamically → Q10 or CLOSE
    "Q10": {"*": "CLOSE"},
}

# If elapsed seconds < TIME_CHECK_THRESHOLD when Q9 is answered, ask Q10.
TIME_CHECK_THRESHOLD = 240  # 4 minutes (per routing-logic.mermaid)

TERMINAL_STATES = {"CLOSE", "CALLBACK"}

# System audio files
SYS_AUDIO = {
    "OPEN":     "SYS_001.wav",   # opening greeting + "is now a good time?"
    "CALLBACK": "SYS_002.wav",   # "we'll reach out another time, goodbye"
    "REPROMPT": "SYS_003.wav",   # "I didn't catch that, could you repeat?"
    "ACK":      "SYS_004.wav",   # brief acknowledgement / transition
    "CLOSE":    "SYS_005.wav",   # closing thank-you
}


def get_next_question_id(current_id: str, answer_key: str, elapsed_seconds: float) -> str:
    """Resolve the next question ID from the routing table."""
    routes = ROUTING.get(current_id, {})
    next_id = routes.get(answer_key) or routes.get("*") or "CLOSE"

    if next_id == "TIME_CHECK":
        return "Q10" if elapsed_seconds < TIME_CHECK_THRESHOLD else "CLOSE"

    return next_id
