"""
Single source of truth for the 12-step outreach cadence and outcome enumerations.
Imported by app.py and scripts/daily_pipeline.py.
"""

CADENCE = [
    {"step": 1,  "day": 1,  "type": "call",    "label": "Call"},
    {"step": 2,  "day": 1,  "type": "vm",      "label": "Leave Voicemail"},
    {"step": 3,  "day": 1,  "type": "email",   "label": "Send Intro Email"},
    {"step": 4,  "day": 3,  "type": "call",    "label": "Call"},
    {"step": 5,  "day": 3,  "type": "message", "label": "Short Connect Message"},
    {"step": 6,  "day": 3,  "type": "log",     "label": "Log Day 3 Outcome"},
    {"step": 7,  "day": 6,  "type": "call",    "label": "Call"},
    {"step": 8,  "day": 6,  "type": "email",   "label": "Bump Email"},
    {"step": 9,  "day": 9,  "type": "call",    "label": "Call"},
    {"step": 10, "day": 9,  "type": "email",   "label": "Reference Email"},
    {"step": 11, "day": 12, "type": "call",    "label": "Final Call"},
    {"step": 12, "day": 12, "type": "email",   "label": "Close Loop Email"},
]

CALL_OUTCOMES  = ["connected", "no_answer", "vm_left", "wrong_number", "callback_scheduled"]
EMAIL_OUTCOMES = ["sent", "replied", "bounced", "opened"]
FINAL_OUTCOMES = ["converted", "nurture_90", "dead", "no_contact"]
