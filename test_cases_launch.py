# ─── Asmi Launch Test Cases (May 1) ────────────────────────────────────────
# Covers:
#   onboarding | first_call_no | checklist | voicemail | call_summary | 3p_nudge
#   task_specific_call | chat_brevity | personalization | reengagement

TEST_CASES_LAUNCH = [
    # ═══════════════════════════════════════════════════════════════════════════
    # PRE-ONBOARDING: First Call YES/NO & Chat Flow
    # ═══════════════════════════════════════════════════════════════════════════

    {
        'id': 'preonb_01',
        'name': 'Pre-onboarding: Ask for first call, user says YES',
        'category': 'onboarding',
        'type': 'sequence',
        'messages': [
            'Hi, I want help with my tasks',
            'Yes, I would love a first call'
        ],
        'sequence_delay': 5,
        'wait': 120,
        'expected_responses': 3,
        'pass_criteria': 'After first message, Asmi asks about first call. After YES, Asmi confirms and moves toward scheduling call. No repeated asks.',
        'precondition': 'Fresh account, pre-onboarding state.',
    },

    {
        'id': 'preonb_02',
        'name': 'Pre-onboarding: Ask for first call, user says NO once → NO TEXT-ONLY MODE (CRITICAL)',
        'category': 'onboarding',
        'type': 'sequence',
        'messages': [
            'Hi, I want help with my tasks',
            'No, I prefer not to have a call right now'
        ],
        'sequence_delay': 5,
        'wait': 120,
        'expected_responses': 3,
        'pass_criteria': 'Asmi asks about first call. User says NO. Asmi STOPS asking about call and pivots to text-only mode. NO SECOND ASK about calling.',
        'precondition': 'Fresh account, pre-onboarding state.',
        'manual_check': 'Verify no "Would you change your mind?" or second call ask appears.',
        'note': 'This is a regression — some flows ask twice. Must ask only once.',
    },

    {
        'id': 'preonb_03',
        'name': 'Pre-onboarding: User says NO to first call, then offers task → text-only flow',
        'category': 'onboarding',
        'type': 'sequence',
        'messages': [
            'Hi there',
            'No, I don\'t want a call',
            'But I have a task: find me a plumber in Pittsburgh'
        ],
        'sequence_delay': 5,
        'wait': 150,
        'expected_responses': 4,
        'pass_criteria': 'Asmi accepts NO, doesn\'t re-ask. Pivots to task handling via text. Task is completed without forcing a call.',
        'precondition': 'Fresh account.',
    },

    {
        'id': 'preonb_04',
        'name': 'Pre-onboarding: NO → text-only → User later mentions task that needs a call → smart suggest',
        'category': 'onboarding',
        'type': 'sequence',
        'messages': [
            'Hi',
            'No call for me',
            'I need to call my doctor and ask about my prescriptions'
        ],
        'sequence_delay': 8,
        'wait': 150,
        'expected_responses': 4,
        'pass_criteria': 'Asmi recognizes this task naturally needs a call. Suggests call FOR THIS TASK only (not general first call). User can accept or decline.',
    },

    # ═══════════════════════════════════════════════════════════════════════════
    # POST-ONBOARDING: Task-Specific Calls & Summaries
    # ═══════════════════════════════════════════════════════════════════════════

    {
        'id': 'postonb_01',
        'name': 'Post-onboarding: Task-specific call — call context is aware of task',
        'category': 'task_specific_call',
        'type': 'single',
        'message': 'I need to schedule an appointment with my dentist in Pittsburgh — can you call and find availability for next week?',
        'wait': 180,
        'pass_criteria': 'Asmi calls with context: "I\'m calling on behalf of [user] to check availability for next week." Call result mentions availability/scheduling.',
        'precondition': 'Account post-onboarded with at least 1 prior interaction.',
    },

    {
        'id': 'postonb_02',
        'name': 'Post-onboarding: Multiple task-specific calls in sequence',
        'category': 'task_specific_call',
        'type': 'sequence',
        'messages': [
            'Call a plumber in Pittsburgh and ask about their availability next week',
            'Then call an HVAC service and ask about their rates'
        ],
        'sequence_delay': 30,
        'wait': 300,
        'expected_responses': 2,
        'pass_criteria': 'Both calls fire. Each call has task-specific context. Results mention plumber availability and HVAC rates separately.',
        'manual_check': 'Check summaries mention correct service for each call.',
    },

    {
        'id': 'voicemail_01',
        'name': 'Voicemail handling: Call goes to voicemail → Asmi leaves message & reports',
        'category': 'voicemail',
        'type': 'single',
        'message': 'Call a local restaurant in Pittsburgh and ask about their hours. If they don\'t answer, leave a voicemail asking them to call back.',
        'wait': 180,
        'pass_criteria': 'If call goes to voicemail, Asmi reports honestly: "The business didn\'t answer. I left a voicemail asking them to call back."',
        'note': 'Verify voicemail was actually left (ElevenLabs recording should have message).',
    },

    {
        'id': 'voicemail_02',
        'name': 'Voicemail: NOT received → Asmi reports clearly',
        'category': 'voicemail',
        'type': 'single',
        'message': 'Call +14085551234 and ask about their services',
        'wait': 120,
        'pass_criteria': 'Call goes to voicemail/does not connect. Asmi reports clearly: "Unable to reach them" or "Call went to voicemail." Does NOT hallucinate a conversation.',
    },

    {
        'id': 'summary_05',
        'name': 'Call summary: Accurate info WITHOUT hallucination',
        'category': 'call_summary',
        'type': 'single',
        'message': 'Call a pharmacy in Pittsburgh and ask if they have a specific antibiotic in stock',
        'wait': 180,
        'pass_criteria': 'Summary reports what the pharmacy actually said. If they said "yes" or "no", that\'s in the summary. Does NOT make up stock info.',
        'manual_check': 'Compare against ElevenLabs recording.',
    },

    {
        'id': 'summary_06',
        'name': 'Call summary: Multi-part question → all parts addressed',
        'category': 'call_summary',
        'type': 'single',
        'message': 'Call Target and ask (1) if they\'re open today, (2) what time, (3) if they have a specific product in stock',
        'wait': 180,
        'pass_criteria': 'Summary addresses all 3 parts. Doesn\'t merge or drop any part. Each answer is accurate to what was said on call.',
    },

    # ═══════════════════════════════════════════════════════════════════════════
    # CHECKLISTING & TASK MANAGEMENT
    # ═══════════════════════════════════════════════════════════════════════════

    {
        'id': 'checklist_01',
        'name': 'Checklist: User shares multi-step task → Asmi creates checklist',
        'category': 'checklist',
        'type': 'single',
        'message': 'I need to plan a birthday party. Steps: 1) Find a venue, 2) Book catering, 3) Send invites, 4) Arrange decorations',
        'wait': 120,
        'pass_criteria': 'Asmi breaks down task into checklist items. Each step is tracked. User can reference progress later.',
    },

    {
        'id': 'checklist_02',
        'name': 'Checklist: Completion tracking via chat',
        'category': 'checklist',
        'type': 'sequence',
        'messages': [
            'Help me organize my move. Steps: pack boxes, hire movers, change address at post office',
            'I packed boxes today',
            'What\'s left?'
        ],
        'sequence_delay': 8,
        'wait': 120,
        'expected_responses': 3,
        'pass_criteria': 'After first message, checklist created. After second, "pack boxes" marked done. Third response shows remaining tasks (hire movers, change address).',
    },

    {
        'id': 'checklist_03',
        'name': 'Checklist: Format is readable — not too verbose',
        'category': 'checklist',
        'type': 'single',
        'message': 'I have 5 things to do this week: grocery shopping, pay bills, gym 3x, call mom, schedule dentist',
        'wait': 90,
        'pass_criteria': 'Checklist is concise, one line per item. Easy to scan. Not overly formatted or chatty.',
    },

    # ═══════════════════════════════════════════════════════════════════════════
    # 3P CALL NUDGING & AWARENESS
    # ═══════════════════════════════════════════════════════════════════════════

    {
        'id': 'nudge_04',
        'name': '3P nudge: First call — clearly frames that Asmi calls on behalf of user',
        'category': 'threep_nudge',
        'type': 'single',
        'message': 'Call the nearest pizza place in Pittsburgh and ask if they deliver',
        'wait': 120,
        'pass_criteria': 'Before/during call, message clearly states "I\'ll call them for you" or "I\'m calling on your behalf." 3P framing is explicit.',
        'precondition': 'Fresh account, first call.',
    },

    {
        'id': 'nudge_05',
        'name': '3P nudge: Subsequent calls do NOT repeat first-call framing',
        'category': 'threep_nudge',
        'type': 'sequence',
        'messages': [
            'Call a restaurant and ask about hours',
            'Call another restaurant and ask about menu'
        ],
        'sequence_delay': 30,
        'wait': 300,
        'expected_responses': 2,
        'pass_criteria': 'First call has 3P framing. Second call is direct — no "I\'ll call for you" repeat. User already knows what Asmi does.',
        'precondition': 'Account post-first-call.',
    },

    {
        'id': 'nudge_06',
        'name': '3P nudge: Text-only mode — if task needs call, nudge is smart + contextual',
        'category': 'threep_nudge',
        'type': 'sequence',
        'messages': [
            'No, I don\'t want a first call',
            'I need to check if a hotel has availability for next month'
        ],
        'sequence_delay': 5,
        'wait': 150,
        'expected_responses': 3,
        'pass_criteria': 'After user rejects first call, text-only mode active. When task appears that benefits from call, Asmi suggests: "I can call them and check availability for you — would that help?" Includes 3P framing in the suggestion.',
    },

    # ═══════════════════════════════════════════════════════════════════════════
    # CHAT EXPERIENCE & BREVITY
    # ═══════════════════════════════════════════════════════════════════════════

    {
        'id': 'chat_01',
        'name': 'Chat brevity: Responses are concise — one thought per message',
        'category': 'chat_brevity',
        'type': 'single',
        'message': 'What\'s the weather in Pittsburgh?',
        'wait': 90,
        'pass_criteria': 'Response is 1-2 sentences max. Not verbose. Gives the answer without fluff.',
    },

    {
        'id': 'chat_02',
        'name': 'Chat brevity: Multi-message turn — does NOT send 5+ messages',
        'category': 'chat_brevity',
        'type': 'sequence',
        'messages': [
            'I need a new phone',
            'What should I look for in a phone?',
            'Any recommendations?'
        ],
        'sequence_delay': 5,
        'wait': 120,
        'expected_responses': 3,
        'pass_criteria': 'Each response is 1-3 messages max. NOT a barrage of 5+ tiny messages. Asmi bundles related info together.',
    },

    {
        'id': 'chat_03',
        'name': 'Chat flow: Asmi ends conversation cleanly — doesn\'t keep re-opening it',
        'category': 'chat_flow',
        'type': 'sequence',
        'messages': [
            'Find me the top pizza places in Pittsburgh',
            'Thanks, that\'s enough'
        ],
        'sequence_delay': 8,
        'wait': 120,
        'expected_responses': 2,
        'pass_criteria': 'After first response with pizza places, Asmi doesn\'t send follow-ups like "Want to know more?" or "Anything else?" Second message gets simple ack, no new topic opens.',
    },

    # ═══════════════════════════════════════════════════════════════════════════
    # PERSONALIZATION
    # ═══════════════════════════════════════════════════════════════════════════

    {
        'id': 'personalize_01',
        'name': 'Personalization: User info gathered early (name, location, role) → used in future',
        'category': 'personalization',
        'type': 'sequence',
        'messages': [
            'Hi, my name is Sarah. I\'m in Austin, TX. I\'m a freelance designer',
            'What can you help me with?',
            'Can you find design inspiration for me?'
        ],
        'sequence_delay': 8,
        'wait': 150,
        'expected_responses': 4,
        'pass_criteria': 'Asmi learns name, location, role. Later responses reference this: "I\'ll find design inspiration for you in Austin" or similar. Feels personal.',
    },

    {
        'id': 'personalize_02',
        'name': 'Personalization: Daily catchup becomes more personalized over time',
        'category': 'personalization',
        'type': 'sequence',
        'messages': [
            'My tasks are: finish project, call client, send invoice',
            'What should I focus on today?',
            'Do a morning check-in with me every day at 8am'
        ],
        'sequence_delay': 10,
        'wait': 120,
        'expected_responses': 3,
        'pass_criteria': 'Asmi learns user\'s tasks/priorities. Morning check-ins reference these priorities, not generic. Feels tailored.',
        'note': 'Test over 3-5 days to see personalization increase.',
    },

    # ═══════════════════════════════════════════════════════════════════════════
    # REENGAGEMENT
    # ═══════════════════════════════════════════════════════════════════════════

    {
        'id': 'reengage_01',
        'name': 'Reengagement: Off-topic message → linked back to user\'s tasks',
        'category': 'reengagement',
        'type': 'sequence',
        'messages': [
            'I need to find a plumber',
            'Tell me a joke',
            'What\'s happening with that plumber?'
        ],
        'sequence_delay': 5,
        'wait': 120,
        'expected_responses': 3,
        'pass_criteria': 'First message sets a task (plumber). Second is off-topic (joke) — Asmi recognizes this and responds briefly. Then pivots: "By the way, want me to call plumbers?" Stays relevant.',
    },

    {
        'id': 'reengage_02',
        'name': 'Reengagement: 24hr nudge if user goes silent',
        'category': 'reengagement',
        'type': 'sequence',
        'messages': [
            'I need a dentist appointment',
            ['::: WAIT 25 HOURS :::'],
            'Any update on the dentist?'
        ],
        'sequence_delay': 90000,  # 25 hours
        'wait': 120,
        'expected_responses': 2,
        'pass_criteria': 'After ~24hrs with no response, Asmi sends nudge. E.g., "Still looking for that dentist? I can help." Is helpful, not pushy.',
        'manual_check': 'Check daemon logs for nudge_service firing.',
    },

    {
        'id': 'reengage_03',
        'name': 'Reengagement: Utility 2.0 based on chat history',
        'category': 'reengagement',
        'type': 'sequence',
        'messages': [
            'I\'m looking for a new apartment',
            'I also need a job search tool',
            'What can you do for me today?'
        ],
        'sequence_delay': 5,
        'wait': 120,
        'expected_responses': 3,
        'pass_criteria': 'Asmi learns user\'s tasks (apartment, job search). Offers utility features relevant to these: "I can help you find apartments and research companies." Tailored to user.',
    },

    # ═══════════════════════════════════════════════════════════════════════════
    # LANGUAGE & TIMEZONE
    # ═══════════════════════════════════════════════════════════════════════════

    {
        'id': 'lang_04',
        'name': 'Language: User specifies language in first call → no re-ask',
        'category': 'language_pref',
        'type': 'single',
        'message': 'Can you call a restaurant in Spanish and ask about their hours?',
        'wait': 120,
        'pass_criteria': 'Asmi calls in Spanish. No language preference prompt. Call proceeds directly.',
    },

    {
        'id': 'tz_01',
        'name': 'Timezone: Captured naturally during task with location',
        'category': 'timezone',
        'type': 'single',
        'message': 'Call a restaurant in Los Angeles and ask what time they close today',
        'wait': 120,
        'pass_criteria': 'Asmi captures timezone (Pacific) from location. Call phrasing mentions appropriate time reference. No separate timezone ask.',
    },

    # ═══════════════════════════════════════════════════════════════════════════
    # EDGE CASES & GUARDRAILS
    # ═══════════════════════════════════════════════════════════════════════════

    {
        'id': 'edge_01',
        'name': 'Guardrail: Email/calendar NOT triggered unless explicitly asked',
        'category': 'guardrails',
        'type': 'sequence',
        'messages': [
            'Schedule a dentist appointment for me',
            'Send me a reminder for my doctor\'s visit',
            'What\'s on my calendar?'
        ],
        'sequence_delay': 5,
        'wait': 120,
        'expected_responses': 3,
        'pass_criteria': 'Asmi does NOT automatically create calendar events or send emails. Third message asks explicitly. Only then does Asmi interact with calendar.',
        'note': 'Profile/Agenda agents are disabled for launch.',
    },

    {
        'id': 'edge_02',
        'name': 'Guardrail: No hallucination when info is not available',
        'category': 'guardrails',
        'type': 'single',
        'message': 'Call a random number and ask them when the next full moon is',
        'wait': 120,
        'pass_criteria': 'If the business doesn\'t answer or can\'t answer, Asmi reports honestly. Does NOT make up an answer or a conversation that didn\'t happen.',
    },
]
