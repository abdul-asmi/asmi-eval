# Asmi Launch Test Case Guide

## 🎯 Test Case Schema (Your Format)

Your test cases follow this Python dictionary structure. Here's how to configure each parameter:

```python
TEST_CASE = {
    'id': 'preonb_02',                    # Unique identifier (category_number)
    'name': 'Ask once, user says NO → no second ask',  # Brief description
    'category': 'onboarding',             # Test category
    'type': 'sequence',                   # How the test flows: single|burst|sequence|dedup|burst_with_setup
    
    # MESSAGE(S)
    'message': 'Single message text',           # For 'single' type
    'messages': ['msg1', 'msg2', 'msg3'],       # For 'burst'/'sequence' type
    
    # TIMING
    'sequence_delay': 5,                  # Seconds between sequential messages
    'burst_delay': 1,                     # Seconds between burst messages
    'wait': 120,                          # Total timeout in seconds for response
    
    # EXPECTATIONS
    'expected_responses': 3,              # Expected number of responses
    'pass_criteria': 'Asmi stops asking about call after user says NO once.',  # What success looks like
    
    # OPTIONAL
    'precondition': 'Fresh account, pre-onboarding state.',  # Setup state
    'manual_check': 'Verify no second call ask appears.',  # Manual verification steps
    'note': 'Regression test — some flows ask twice.',  # Additional context
}
```

---

## 📊 Parameter Reference

### `type` (How the test flows)

| Type | Use When | Example |
|------|----------|---------|
| `single` | Send one message, wait for response(s) | "What can you do?" |
| `sequence` | Send messages sequentially with delays | Message 1 → wait → Message 2 → wait → Message 3 |
| `burst` | Send multiple messages rapidly back-to-back | Send 4 messages in quick succession (1-2 sec apart) |
| `dedup` | Test duplicate detection | Send same task twice within N seconds, verify only one fires |
| `burst_with_setup` | Setup message first, then burst | Send intro, wait, then send 4 burst messages |

### `category` (What area of the system)

- **onboarding** - Pre/post onboarding flows, first call yes/no
- **call_experience** - Call execution, summaries, context, nudging
- **chat_experience** - Message brevity, conversation flow, checkpointing
- **checklist** - Task tracking, completion, formatting
- **voicemail** - Voicemail handling, reporting
- **call_summary** - Call result accuracy, hallucination prevention
- **threep_nudge** - 3P call framing and awareness
- **personalization** - User learning, tailored responses
- **reengagement** - Dormant user handling, nudges
- **language_pref** - Language selection, no re-asking
- **location_memory** - Location retention across tasks
- **task_specific_call** - Call context from task
- **guardrails** - Email/calendar triggers, accuracy
- **capability** - What Asmi can/cannot do
- **timezone** - Timezone detection and usage

---

## 🔧 How to Configure Tests Easily

### Option 1: Add to `test_cases_launch.py`
```python
{
    'id': 'your_test_01',
    'name': 'Clear, descriptive name',
    'category': 'onboarding',
    'type': 'sequence',
    'messages': [
        'First message to send',
        'Second message after delay'
    ],
    'sequence_delay': 5,
    'wait': 120,
    'expected_responses': 2,
    'pass_criteria': 'What the test should verify',
},
```

### Option 2: Use the Interactive Board
📌 **View the planning board:**
- Open `test_planning_board.html` in your browser
- Click `+ Add test` in any category
- Fill in: Name, Description, Priority, Status
- Tests update in real-time
- See coverage gaps on the right

### Option 3: Run via Railway UI
1. Go to `https://web-production-a1a67.up.railway.app`
2. Select a test category
3. Click "Run this test"
4. Asmi sends messages, judge evaluates, results display inline

---

## 🚀 How to Run Tests

### 1. Single Test via iMessage
Send to Asmi (+14082307921):
```
!run preonb_02
```

### 2. Category via iMessage
```
!run onboarding
```

### 3. All Tests
```
!run all
```

### 4. Via Railway UI (Recommended)
- Navigate to the test in the UI
- Click "Run this test"
- See result inline (once inline results are fully live)

---

## 🎯 Critical Launch Tests (Priorities)

### P0 (Must Pass Before Launch)
- ✅ **preonb_02** - User says NO to first call → STOPS asking (no double ask)
- ✅ **preonb_03** - User NO → transitions to text-only mode
- ✅ **postonb_01** - Task-specific calls have context
- ✅ **voicemail_01/02** - Voicemail honestly reported
- ✅ **checklist_01/02** - Checklisting works
- ✅ **chat_01/02** - Message brevity enforced
- ✅ **edge_01/02** - No email/calendar auto-trigger, no hallucination

### P1 (Important, can ship with known gaps)
- ⚠️ **reengage_02** - 24hr nudge system
- ⚠️ **reengage_03** - Utility 2.0 based on chat
- ⚠️ Multi-iteration calls

---

## 📋 Test Case Examples

### Example 1: Simple Single Message
```python
{
    'id': 'chat_01',
    'name': 'Chat brevity: Responses are concise',
    'category': 'chat_experience',
    'type': 'single',
    'message': 'What\'s the weather in Pittsburgh?',
    'wait': 90,
    'pass_criteria': 'Response is 1-2 sentences max. Not verbose.',
}
```

### Example 2: Sequence (Multi-step flow)
```python
{
    'id': 'preonb_02',
    'name': 'Ask once, user says NO → no second ask',
    'category': 'onboarding',
    'type': 'sequence',
    'messages': [
        'Hi, I want help with my tasks',
        'No, I prefer not to have a call'
    ],
    'sequence_delay': 5,
    'wait': 120,
    'expected_responses': 3,
    'pass_criteria': 'Asmi asks about first call. User says NO. Asmi STOPS asking — NO SECOND ASK.',
    'precondition': 'Fresh account, pre-onboarding state.',
    'manual_check': 'Verify no "Would you change your mind?" appears.',
}
```

### Example 3: Burst (Rapid fire)
```python
{
    'id': 'nudge_06',
    'name': '3P nudge: Text-only mode → smart task nudge',
    'category': 'threep_nudge',
    'type': 'burst',
    'messages': [
        'No, I don\'t want a first call',
        'I need to check if a hotel has availability'
    ],
    'burst_delay': 1,
    'wait': 150,
    'expected_responses': 2,
    'pass_criteria': 'NO mode active. When task that needs call appears, Asmi suggests: "I can call them..." with 3P framing.',
}
```

---

## 🔍 How to Check Test Results

### Via Results JSON
Results saved to `/eval/reports/results_*.json`

Each result contains:
```json
{
    "id": "preonb_02",
    "name": "Ask once, user says NO → no second ask",
    "tasks_sent": ["Hi, I want help with my tasks", "No, I prefer..."],
    "responses": ["Asmi response 1", "Asmi response 2", ...],
    "verdict": "PASS" | "FAIL",
    "reason": "Why it passed or failed",
    "matched_responses": "Which responses matched criteria",
    "started_at": "2026-05-05T...",
    "finished_at": "2026-05-05T..."
}
```

### Via Railway UI
- Click into any test result
- See:
  - Messages sent ✉️
  - Asmi's responses 💬
  - Judge verdict ✅/❌
  - Detailed reason

### Via Daemon Logs
```bash
tail -f ~/Desktop/asmi/eval/daemon.log
```
Look for test execution logs.

---

## 🎨 Test Case Planning Board Features

### Interactive Board
Open `test_planning_board.html` to:
- 📌 **Organize** test cases by category (drag/drop coming soon)
- ➕ **Add new tests** without coding
- ✅ **Track status** (Pending → In Progress → Done)
- 📊 **See coverage gaps** listed on the right
- 🔴 **Identify P0 vs P1** at a glance

### Coverage Gaps Shown
- Text-only onboarding flow
- Call dedup for rapid fire
- Multi-iteration calls
- Channel latency testing
- Feedback loop + incentives
- Referral code system
- Language preference handling
- Timezone awareness

Add these to the board when ready!

---

## 📝 Quick Checklist Before Launch

- [ ] All P0 tests passing
- [ ] Double-ask regression fixed (preonb_02)
- [ ] Text-only mode working (preonb_03/04)
- [ ] Checklisting functional
- [ ] Voicemail handling accurate
- [ ] 3P nudging consistent
- [ ] No email/calendar auto-triggers
- [ ] Call summaries not hallucinating
- [ ] Chat is brief (no barrage)
- [ ] Personalization starting to work

---

## 🚨 How to Configure & Fix Issues

### Issue: Test keeps timing out
**Fix:** Increase `wait` time or check if Asmi is responding at all
```python
'wait': 240,  # Increase from 120
```

### Issue: Expected responses count is wrong
**Fix:** Check the `responses` array in the result JSON
```python
'expected_responses': 4,  # Change to actual count received
```

### Issue: Pass criteria too strict
**Fix:** Refine the criteria language
```python
'pass_criteria': 'Asmi responds with weather info within 90 seconds.',
```

### Issue: Need manual verification
**Add a note:**
```python
'manual_check': 'Compare iMessage timestamps to verify no double ask.',
'note': 'This is a regression test.',
```

---

## 📚 Full Test File Location
- **Launch tests:** `/Users/yaybeedee/Desktop/asmi/eval/test_cases_launch.py`
- **Existing tests:** `/Users/yaybeedee/Desktop/asmi/eval/test_cases.py`
- **Plan board:** `/Users/yaybeedee/Desktop/asmi/eval/test_planning_board.html`

---

## 🎯 Next Steps

1. **Review** `test_cases_launch.py` - matches your schema exactly
2. **Open** `test_planning_board.html` in browser - interactive planning board
3. **Run** tests via Railway UI or iMessage commands
4. **Track** results in the board / JSON files
5. **Fix** regressions as they appear
6. **Repeat** until all P0s pass

---

Questions? The schema is simple — just follow the examples above and you're good to go! 🚀
