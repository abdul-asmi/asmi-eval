# ─── Asmi Regression Test Cases ──────────────────────────────────────────────
# Covers v0.10.37.2 changelog:
#   sticky_message | call_dedup | call_summary | language_pref
#   location_memory | onboarding | capability | threep_nudge

TEST_CASES = [
    {
        'id': '3pc_01',
        'name': 'Number in message → call placed',
        'category': '3P Calls',
        'type': 'sequence',
        'messages': ['cmd_reset_history', 'Call nearest chinese restaurant and book a table for 2 for 6PM'],
        'pass_criteria': 'Asmi responds in chat confirming it found a number or is looking one up. Does not ask the user to provide the number.',
    },
    {
        'id': '3pc_02',
        'name': 'Name only → asks for number',
        'category': '3P Calls',
        'type': 'sequence',
        'messages': ['cmd_reset_history', "Call Joe's Pizza and ask about their catering options."],
        'pass_criteria': 'Asmi either finds a number via web search and confirms in chat, or asks the user for the number. Does not silently fail.',
    },
    {
        'id': '3pc_03',
        'name': 'Quiet hours → override accepted',
        'category': '3P Calls',
        'type': 'sequence',
        'messages': ['cmd_reset_history', 'Call (412) 555-0123 and ask if they have a table for 2 tonight at 7pm.', 'call anyway'],
        'pass_criteria': "Asmi's first chat message flags quiet hours. After 'call anyway', Asmi confirms it will proceed — no re-prompting.",
        'manual_check': 'Run after 10pm local time.',
    },
    {
        'id': '3pc_04',
        'name': 'Quiet hours → override declined',
        'category': '3P Calls',
        'type': 'sequence',
        'messages': ['cmd_reset_history', 'Call (412) 555-0199 right now.', "don't bother"],
        'pass_criteria': "Asmi's first chat message flags quiet hours. After 'don't bother', Asmi confirms no call will be made. No further prompting.",
        'manual_check': 'Run after 10pm local time.',
    },
    {
        'id': '3pc_05',
        'name': "3P doesn't answer",
        'category': '3P Calls',
        'type': 'sequence',
        'messages': ['cmd_reset_history', 'Call (412) 555-0000 and ask about their hours.'],
        'pass_criteria': 'Asmi sends a chat message reporting no answer. Message should offer to retry or ask how to proceed — not go silent.',
    },
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
