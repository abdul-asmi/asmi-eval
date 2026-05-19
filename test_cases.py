# ─── Asmi Regression Test Cases ──────────────────────────────────────────────
# Covers v0.10.37.2 changelog:
#   sticky_message | call_dedup | call_summary | language_pref
#   location_memory | onboarding | capability | threep_nudge

TEST_CASES = [
    {
        'id': 'pdf_01',
        'name': 'PDF from chat history',
        'category': 'PDF',
        'type': 'sequence',
        'messages': ['cmd_reset_history', "Can you help me book a flight to Honalulu from Hyderabad departing June 1st to June 10th. Stopovers are okay, budget is $50000. This is round-trip. Economy. No other constraints. I also want to book hotels during the same time. 3+ star rating with a gym. Up to $300/night. No other constraints. Let's do this entirely via chat. No call please."],
        'pass_criteria': 'Asmi responds in chat with research, options, or next steps. If PDF is generated, a file is delivered in chat.',
        'wait': 20,
    },
    {
        'id': 'pdf_02',
        'name': 'PDF from call content',
        'category': 'PDF',
        'type': 'sequence',
        'messages': ['cmd_reset_history', 'Make me a plan PDF from what we just discussed.'],
        'pass_criteria': "Asmi either delivers a PDF file in chat or explains why one can't be generated. Should not confirm delivery if no file arrives.",
        'precondition': 'Discuss some task in call',
        'manual_check': 'Run after completing an ad hoc call in the same session.',
    },
    {
        'id': 'pdf_03',
        'name': 'False confirm — no delivery (regression)',
        'category': 'PDF',
        'type': 'sequence',
        'messages': ['cmd_reset_history', 'Can you send me a PDF summary of my tasks?'],
        'pass_criteria': "REGRESSION: If Asmi says 'Sending now' or 'Here you go' but no PDF file arrives in chat within 60s, flag as bug.",
    },
    {
        'id': 'pdf_04',
        'name': 'Multi-task PDF with status',
        'category': 'PDF',
        'type': 'sequence',
        'messages': ['cmd_reset_history', 'Find me flights from Pittsburgh to NYC next Friday under $200.', 'Also add oat milk and greek yogurt to my grocery list.', "Now give me a PDF of everything we've worked on with their status."],
        'pass_criteria': 'PDF delivered as file in chat. Covers both tasks (flight search + grocery list) with status for each.',
    },
]
