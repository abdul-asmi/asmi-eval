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
]
