# Asmi iMessage Eval System — Living Guide

> **How to use this doc:** Every time you get a new changelog, new use cases, or new bugs,
> come here first. Add your test cases, run the suite, read the report. That's the loop.

---

## 1. Quick Start (2-minute refresher)

```bash
# One-time setup
cd /Users/yaybeedee/Desktop/asmi/eval
pip install google-generativeai

# ⚠ Required once: Terminal needs Full Disk Access to read iMessages
# System Settings → Privacy & Security → Full Disk Access → enable Terminal

# See all 28 tests
python run_eval.py --list

# Run everything
python run_eval.py

# Run one category
python run_eval.py --category sticky_message
python run_eval.py --category call_dedup
python run_eval.py --category call_summary
python run_eval.py --category language_pref
python run_eval.py --category location_memory
python run_eval.py --category onboarding
python run_eval.py --category capability
python run_eval.py --category threep_nudge

# Run one specific test
python run_eval.py --id sticky_03

# Open the HTML report (auto-generated after each run)
open report_YYYYMMDD_HHMM.html
```

**Typical run time:** ~15–20 min for the full suite (most time is waiting for Asmi to respond).
**For a quick regression check:** run `sticky_message` + `call_dedup` only (~5 min).

---

## 2. System Overview

```
eval/
├── EVAL_GUIDE.md      ← you are here — living doc, update this every sprint
├── config.py          ← Asmi's number, Gemini key, timeouts
├── test_cases.py      ← ALL test definitions — edit this to add/update tests
├── imessage.py        ← sends via AppleScript, reads via chat.db (don't touch)
├── judge.py           ← Gemini 1.5 Flash scores responses (don't touch)
├── runner.py          ← orchestrates test types (don't touch)
├── report.py          ← generates HTML report (don't touch)
├── run_eval.py        ← CLI entry point (don't touch)
└── requirements.txt   ← pip dependencies
```

**The only two files you'll ever edit:** `test_cases.py` and `EVAL_GUIDE.md`.

---

## 3. How to Add or Update Test Cases

### 3.1 Open `test_cases.py` and find the right category section

Each category has a block marked with a header like:
```python
# ══ CATEGORY 1 — STICKY MESSAGE ══
```

Add your new test case inside the `TEST_CASES = [...]` list, in the right category block.

### 3.2 Pick the right test type

| Type | When to use | What it does |
|---|---|---|
| `single` | One message, one response | Sends 1 message, waits for 1 reply |
| `burst` | Rapid-fire multiple messages | Sends N messages fast, waits for N replies |
| `burst_with_setup` | Need context set first | Sends a setup message, waits, then bursts |
| `sequence` | Step-by-step with replies | Sends 1, waits for reply, sends next |
| `dedup` | Duplicate detection | Sends msg twice, confirms only 1 response |

### 3.3 Copy the right template

**Single test:**
```python
{
    "id": "my_cat_XX",                     # unique ID: category prefix + number
    "name": "Short description of what is being tested",
    "category": "sticky_message",          # see category list below
    "type": "single",
    "message": "The exact message you'd send to Asmi on iMessage",
    "wait": 120,                           # seconds to wait for response
    "pass_criteria": (
        "What a PASS looks like. Be specific. "
        "The Gemini judge reads this verbatim."
    ),
    # optional fields:
    "precondition": "Fresh account required",   # shown as warning before test runs
    "manual_check": "Check EL dashboard",       # shown in report, not auto-evaluated
    "note": "Lock phone after sending",         # general note shown in report
},
```

**Burst test:**
```python
{
    "id": "my_cat_XX",
    "name": "Burst: N tasks rapid-fire, all arrive",
    "category": "sticky_message",
    "type": "burst",
    "messages": [
        "First message to Asmi",
        "Second message to Asmi",
        "Third message to Asmi",
        "Fourth message to Asmi",
    ],
    "burst_delay": 1.0,                    # seconds between sends (default 1.0)
    "wait": 240,                           # total seconds to wait for all responses
    "expected_responses": 4,              # how many responses you expect back
    "pass_criteria": (
        "All 4 responses arrive. None dropped. Each is relevant to its task."
    ),
},
```

**Sequence test (each message waits for a reply before the next sends):**
```python
{
    "id": "my_cat_XX",
    "name": "Sequence: context retained across steps",
    "category": "location_memory",
    "type": "sequence",
    "messages": [
        "First message — sets context",
        "Second message — uses that context",
        "Third message — should not re-ask for context",
    ],
    "sequence_delay": 12.0,               # seconds to wait between each step
    "wait": 120,                          # timeout per step
    "expected_responses": 3,
    "pass_criteria": (
        "Messages 2 and 3 do not ask for context that was already given in message 1."
    ),
},
```

**Dedup test:**
```python
{
    "id": "my_cat_XX",
    "name": "Duplicate task within Xs → only one fires",
    "category": "call_dedup",
    "type": "dedup",
    "message": "Call a pharmacy and ask about flu shots",
    "dedup_message": "Call a pharmacy and ask about flu shots",  # can be same or similar
    "dedup_delay": 2.0,                   # seconds between the two sends
    "wait": 180,
    "expected_responses": 1,             # we expect exactly 1 back, not 2
    "pass_criteria": "Only one response arrives. Asmi did not make two separate calls.",
    "manual_check": "Check ElevenLabs dashboard — should show 1 call only.",
},
```

### 3.4 Valid categories

```
sticky_message    — message delivery and no-re-prompt
call_dedup        — duplicate call prevention
call_summary      — post-call summary accuracy
language_pref     — no language ask before calls
location_memory   — location retention across tasks
onboarding        — pre-onboarding reaction reliability
capability        — how Asmi describes what it can do
threep_nudge      — 3P call framing on first call
```

To add a **new category** (e.g. for a new feature):
1. Add it to this list in the guide
2. Add it to the `CATEGORIES` dict in `report.py`
3. Add your test cases with the new category key

### 3.5 Writing good pass criteria (most important thing)

The Gemini judge reads your `pass_criteria` verbatim. Write it like you're telling a smart person what to look for.

**Bad:**
```
"Response is good"
"Asmi handles it correctly"
"Works as expected"
```

**Good:**
```
"Asmi responds with at least 3 restaurant names. Includes hours or ratings. 
 Arrives without user needing to send a follow-up. No generic filler text."
```

**Rules:**
- Be specific about what should and should not be in the response
- Mention quantity when it matters ("at least 3", "exactly 1")
- Call out what failure looks like too: "Does NOT ask for location again"
- One criteria string can cover multiple things — just separate with periods

---

## 4. Best Practices for Asmi Eval

These come from LLM eval literature (Anthropic, DeepEval, Confident AI) applied to Asmi's specific architecture.

### 4.1 Test in layers, not just end-to-end

Asmi's response chain: **user message → routing → tool execution (web/call/email) → response formatting → iMessage delivery**

Each layer can fail independently. Your tests should cover:
- Did the response arrive? (delivery layer)
- Was the content correct? (execution + formatting layer)
- Was the action accurate? (tool execution layer — use EL dashboard for calls)
- Was context remembered? (memory layer)

Don't just test the final message — check intermediate signals (EL dashboard, logs) where you can.

### 4.2 Always include burst tests for every new feature

Every feature that processes messages should be tested under rapid-fire load. Asmi users don't wait politely between messages. A feature that works in single-message tests but breaks under burst is not shipped.

**Rule:** For every new `single` test you add, ask yourself: "what breaks if someone sends this 4 times in 2 seconds?" Add a `burst` version if the answer is anything other than "nothing."

### 4.3 Separate detection from diagnosis

Your tests tell you **what** broke, not **why**. When a test fails:
1. Look at the raw response in the JSON results file (`results_YYYYMMDD.json`)
2. Check EL dashboard for call tests
3. Check Asmi's internal logs for routing/tool errors

The eval system is a detector, not a debugger. Once it flags a failure, the debugging is manual.

### 4.4 Use `precondition` for tests that need state you can't automate

Some tests need a fresh account, a first call, or a specific prior interaction. Don't try to automate state resets — just mark the precondition clearly. The runner prints it before the test runs so you can't miss it.

```python
"precondition": "Run this on a fresh account that has never made a call through Asmi."
```

### 4.5 Write tests for bugs first, features second

Every bug Rishi files in `#internal-feedback` should become a test case before the fix ships. This is a regression test, not just a feature test. If it broke once, it will break again.

**Workflow:**
1. Rishi reports bug in Slack
2. You add a test case that would have caught it
3. Fix ships
4. You run that test case to confirm it's resolved
5. Test stays in the suite forever as a regression guard

### 4.6 Tag manual checks clearly — don't skip them

Some things can't be auto-evaluated (EL dashboard, call recordings, delivery timestamps). Use `manual_check` to flag these. The HTML report surfaces them prominently so they don't get missed.

Don't try to fake auto-evaluation for things that need human eyes.

### 4.7 Score by category, not just total

A 90% pass rate overall can hide a 40% pass rate in `call_summary`. Always read the HTML report by category, not just the headline number. If any category is below 80%, that's a ship blocker.

**Suggested thresholds:**
| Category | Min pass rate to ship |
|---|---|
| sticky_message | 100% |
| call_dedup | 100% |
| call_summary | 80% |
| language_pref | 100% |
| location_memory | 80% |
| onboarding | 80% |
| capability | 80% |
| threep_nudge | 100% |

### 4.8 LLM judge calibration

Gemini 1.5 Flash is used as the judge. It's fast and cheap but can be wrong. Rules for trusting the judge:

- **Trust PASS verdicts** if the response in the JSON looks obviously correct
- **Investigate FAIL verdicts** — read the raw response and see if the judge was right
- **Always manually verify UNCLEAR** — these are the edge cases the judge couldn't call
- If the judge consistently gets a specific test wrong, rewrite the `pass_criteria` to be more precise

### 4.9 Timeout tuning

Default timeouts in `config.py`:

```python
RESPONSE_TIMEOUT = 150   # single response
BURST_WAIT       = 240   # waiting for multiple responses
POLL_INTERVAL    = 3     # how often chat.db is checked
```

If you're seeing too many false timeouts (Asmi is responding but tests are failing), increase these. If tests are too slow, decrease `POLL_INTERVAL` to 2.

For call tests specifically, add per-test `"wait": 180` or more — calls take longer than web tasks.

### 4.10 Don't test what you can't repeat

Avoid test cases that require Asmi to call a real business and get a specific answer (e.g., "call and confirm they serve lunch"). Business answers change. Test for **form** (did a summary arrive, was it non-empty, did it not hallucinate) not **content** (did it say exactly "yes we serve lunch").

---

## 5. The Update Workflow (for every changelog)

When Rishi or Satwik shares a new changelog, do this:

```
1. Read the changelog → identify each new behavior or bug fix
2. For each item, ask: "what's the minimum message I can send on iMessage 
   that proves this works?"
3. Add that as a test case in test_cases.py (single first, burst second)
4. Add the regression version: "what would prove this DIDN'T regress?"
5. Run: python run_eval.py --category [new_category]
6. Fix any FAIL or UNCLEAR verdicts
7. Commit test_cases.py with a message like "add tests for v0.10.37.2"
```

**Time budget:** Adding tests for a typical 5-item changelog should take < 30 minutes.

---

## 6. Adding a New Category (when a major new feature ships)

Example: Asmi adds **email sending**. Here's how to add it:

**Step 1 — Add to `test_cases.py`:**
```python
# ══ CATEGORY 9 — EMAIL SENDING ══
{
    "id": "email_01",
    "name": "Email task drafted and confirmed",
    "category": "email_sending",
    "type": "single",
    "message": "Send an email to test@example.com saying I'll be 10 minutes late",
    "wait": 120,
    "pass_criteria": (
        "Asmi confirms it sent or drafted the email. "
        "Mentions the recipient and gist of the message. "
        "Does not ask for clarification not already provided."
    ),
},
```

**Step 2 — Add to `report.py` CATEGORIES dict:**
```python
CATEGORIES = {
    ...
    "email_sending": "Email Sending",
}
```

**Step 3 — Add the category to this guide's category list in section 3.4.**

Done. The runner and report pick it up automatically.

---

## 7. Reading the HTML Report

After each run, open `report_YYYYMMDD_HHMM.html` in your browser.

**Top summary bar:** Total / Passed / Failed / Unclear / Pass rate. This is your headline.

**Category bars:** Each category shows its own pass rate with a color bar.
- Green = 100%
- Yellow = 50–99%
- Red = below 50%

**Individual test rows:** Click to expand. You'll see:
- The exact messages sent
- Asmi's raw responses
- The judge's verdict and reason
- Any manual checks you need to do
- Any preconditions that were required

**JSON results file:** `results_YYYYMMDD.json` — contains every raw response. Use this for debugging. Search by test ID to find the specific responses.

---

## 8. Asmi-Specific Testing Patterns

### 8.1 Testing across the task-completion funnel

Every task Asmi handles goes through: **receive → route → execute → respond**

Test at each stage:
- **Receive:** did iMessage deliver the message? (sticky message tests)
- **Route:** did Asmi understand and route to the right tool? (capability tests)  
- **Execute:** did the action happen correctly? (call summary, dedup tests)
- **Respond:** did the response arrive, make sense, and not hallucinate? (summary tests)

### 8.2 Testing Asmi's memory across tasks

Asmi should remember:
- Location (once given, don't ask again)
- Language preference (captured on call, not before)
- User name and preferences
- Prior task outcomes ("you already called CVS earlier")

For each memory type, the test pattern is always:
1. Provide the information once (sequence step 1)
2. Send tasks that depend on it (steps 2–4)
3. Confirm it was used, not re-asked

### 8.3 Testing call accuracy specifically

Call tests are harder to auto-evaluate because the ground truth is in the audio.
Use this combination:
- **Auto:** did a response arrive? is it non-empty? does it not hallucinate an outcome?
- **Manual:** `manual_check` pointing to EL dashboard link for the call recording

Don't try to auto-evaluate the specific content of call outcomes — too brittle.

### 8.4 What "regression" means for Asmi

A regression is when something that worked in v0.10.X breaks in v0.10.X+1. For Asmi, the highest-risk regressions are:

1. Sticky messages breaking again (delivery layer)
2. Dedup breaking (double-calling a business)
3. Location/language being asked again after being provided
4. Hallucination in call summaries
5. Onboarding stalling on fresh accounts

These are the categories that should be run on **every single build**, not just when those features changed.

---

## 9. Giving Claude (me) a Test Case Update

When you want me to add, update, or remove test cases, paste your update in this format:

```
ADD TEST:
- Category: [category name]
- Type: single / burst / sequence / dedup
- What it tests: [1 sentence]
- Message(s): [exact iMessage text to send]
- Pass criteria: [what a passing response looks like]
- Precondition (if any): [what state is needed]
- Manual check (if any): [what to check outside iMessage]

REMOVE TEST: [test ID]

UPDATE TEST [test ID]:
- Change: [what to change]
```

I'll translate that directly into `test_cases.py` entries. You don't need to write any Python.

---

## 10. Known Limitations

| Limitation | Impact | Workaround |
|---|---|---|
| Can't auto-verify EL call logs | Call dedup and call accuracy need manual verification | `manual_check` fields in report |
| Requires Full Disk Access for Terminal | chat.db read will fail without it | One-time macOS setting |
| Tests run sequentially | Full suite takes ~20 min | Run by category for speed |
| Fresh-account tests can't be automated | State can't be reset mid-suite | Mark as `precondition`, run manually |
| Gemini judge can be wrong | False PASS or FAIL verdicts | Spot-check PASS verdicts, always investigate FAIL |
| Call response time varies | Call tests may timeout on slow days | Increase `wait` per-test if needed |

---

---

## 11. Best Practices from the Source Videos

> **Videos:**
> - [The Most Important New Skill for PMs in 2026: AI Evals Masterclass](https://www.youtube.com/watch?v=Raa3qjEBvKE) — Ankit Shukla, HelloPM
> - [How to Build AI Evals in 2026 (Step-by-Step, No Hype)](https://www.youtube.com/watch?v=J7N9FMouSKg) — Hamel Husain & Shreya Shankar
> - [Why AI Evals Are the Hottest New Skill (Lenny's Podcast)](https://www.lennysnewsletter.com/p/why-ai-evals-are-the-hottest-new-skill) — Hamel Husain & Shreya Shankar

---

### 11.1 The core insight (Ankit Shukla)

> *"AI features don't fail because of the model. They fail because nobody evaluated them."*

Asmi application: every bug in `#internal-feedback` is proof of this. The call deduping bug, the sticky message bug, the hallucinated summaries — none of these are model failures. They're evaluation failures. The eval system is the fix.

**What your eval stack should cover (Ankit's framework applied to Asmi):**

| Dimension | What to measure | How we test it |
|---|---|---|
| Task success | Did Asmi complete the actual task? | LLM judge on response content |
| Hallucination rate | Did Asmi invent information? | `call_summary` tests, judge checks against known facts |
| Delivery reliability | Did the message arrive? | `sticky_message` tests, response count check |
| Latency | How long did it take? | Timestamps in `results_*.json` |
| Context retention | Did it remember what was said? | `location_memory`, `language_pref` sequence tests |
| Dedup accuracy | Did it avoid duplicate actions? | `call_dedup` tests + EL dashboard manual check |

---

### 11.2 Start with error analysis, not infrastructure (Hamel & Shreya)

> *"The main thing inhibiting people is not doing error analysis. Everyone wants to jump to an off-the-shelf metric."*

**What this means for you:** Before writing a new test case, spend 20 minutes reading raw Slack messages in `#internal-feedback` and raw responses in `results_*.json`. What patterns do you see? That pattern is your next category of tests.

**The two-step error analysis process (applied to Asmi):**

**Step 1 — Open coding:** Read 20–30 Asmi responses without trying to categorize them. Write a note next to each: what was wrong, what was right. Don't diagnose yet. Just observe.

Example notes:
```
- Response arrived 3 minutes late → delivery issue
- Said "I cancelled the call" but the call was made → state mismatch
- Asked for location even though I said Pittsburgh → memory failure
- Called Bangalore office instead of national number → search/routing issue
- Summary said "they confirmed availability" but call went to voicemail → hallucination
```

**Step 2 — Axial coding:** Group your notes into 5–6 themes. Those themes become test categories. Right now Asmi has 8 — that came from doing this process on Rishi's Slack messages.

**How often to do this:** Every time a new build ships. Takes 30 minutes. Do it before writing any tests, not after.

---

### 11.3 Build rubrics before writing tests (Ankit Shukla)

> *"A rubric defines what 'good' looks like. Without a rubric, you can't measure quality."*

**The rubric process:**
1. Pick a use case (e.g. "Asmi calls a business and reports back")
2. Write down what the perfect response looks like — specific, not vague
3. Write down what failure looks like — specific, not vague
4. Write down the edge cases that live in between
5. That's your `pass_criteria` string

**Applied to Asmi — rubric for call summary:**

```
GOOD: "I called CVS on Fifth Ave. They confirmed they have walk-in flu shots 
available today until 6pm. No appointment needed."
  → Specific business, specific answer, specific hours, no filler

BAD: "I called and got some information for you."
  → Vague, no actual data from the call

FAIL: "They said they have flu shots and are open until 8pm" (when call 
went to voicemail)
  → Hallucinated outcome — the worst failure mode
```

Turn these three cases into your `pass_criteria`:
```python
"pass_criteria": (
    "Summary includes the specific answer from the business (not vague). "
    "If the call was answered: includes concrete details like hours, availability, price. "
    "If the call was NOT answered: honestly says so — does NOT invent an outcome. "
    "Does not say 'I got some information' without stating what that information is."
),
```

---

### 11.4 Never use agreement as your eval metric (Hamel & Shreya)

> *"A judge that always says PASS can have 90% accuracy if failures are rare. That's a useless judge."*

**The trap:** If 90% of Asmi's responses are good, a judge that always says PASS looks great on paper (90% accuracy). But it catches zero failures.

**What to watch instead — TPR and TNR:**
- **TPR (True Positive Rate):** When a response is actually bad, does the judge catch it? This should be high.
- **TNR (True Negative Rate):** When a response is actually good, does the judge correctly pass it? This should also be high.

**How to calibrate our Gemini judge:**
1. After a run, take 10 PASS verdicts and manually read the responses — did they actually pass?
2. Take all FAIL and UNCLEAR verdicts and manually check — was the judge right?
3. If the judge is letting bad responses through (low TPR), tighten your `pass_criteria`
4. If the judge is failing good responses (low TNR), add more context to `pass_criteria` about what's acceptable

**Practical rule:** Every time a test fails and you fix the bug, re-run that test. If it now passes, the judge is calibrated. If the judge still says FAIL even after the fix, rewrite the criteria.

---

### 11.5 Do the error analysis yourself — don't delegate it (Hamel & Shreya)

> *"PMs must do the error analysis themselves. Engineers lack the domain context."*

**Why this matters for Asmi:** You know what a good Asmi response feels like. Satwik and Sibi know what the system does internally. Those are different things. The eval judgment call — "is this response good enough for a user?" — has to come from you, not from the engineering team.

**Practical rule:** When a test comes back UNCLEAR or you're not sure if a FAIL is a real failure, you read the response and decide. Don't ask Satwik. You are the domain expert for user experience quality.

---

### 11.6 Two eval types — code-based and LLM-based (Hamel & Shreya)

> *"A complete eval suite has 2–3 code-based evals and 1–2 LLM-based evals."*

**Our system uses both:**

| Eval type | What it checks | How it's implemented |
|---|---|---|
| Code-based | Did N responses arrive? Did only 1 fire (dedup)? | `judge_response_count()` in `judge.py` |
| LLM-based | Is the content correct, non-hallucinated, relevant? | Gemini 1.5 Flash in `judge()` in `judge.py` |

**When a code-based check fails, don't even run the LLM judge** — a response that never arrived can't be content-evaluated. This is already how `runner.py` works: count verdict failure overrides LLM verdict.

---

### 11.7 Multi-turn failures compound — test them explicitly (Shreya Shankar + agent eval research)

> *"Failures in conversational agents are experiential — they compound across turns in ways single-turn metrics can't detect."*

This is Asmi's biggest blind spot. A bug that forgets your location in turn 3 won't show up in a single-message test. It only appears across a sequence.

**Applied to Asmi:** The `sequence` test type exists for exactly this. Every memory feature (location, language, name, past task outcomes) needs a sequence test that spans at least 3 turns, not just a single-message test.

**Multi-turn failures to watch for specifically in Asmi:**
- Asking for location after it was given earlier in the conversation
- Asking for language preference after it was set on a prior call
- Starting a new task that conflicts with a prior one without acknowledging it
- Giving a summary that contradicts what the user said earlier in the thread
- Sending multiple "still working on it" updates instead of the actual result

**Test pattern for any new memory feature:**
```python
{
    "type": "sequence",
    "messages": [
        "Seed message that establishes the context/preference",
        "Follow-up that should USE the context (not re-ask)",
        "Third message that should still use it",
    ],
    "pass_criteria": "Messages 2 and 3 do not ask for [context] again.",
}
```

---

### 11.8 Generic metrics are useless for Asmi (Hamel & Shreya)

> *"BERTScore, ROUGE, cosine similarity — not useful for evaluating LLM outputs in most AI applications."*

Never evaluate Asmi responses by comparing them to a "golden output" string. Two reasons:
1. Asmi's correct responses vary in wording — two correct responses can look totally different
2. Asmi's incorrect responses can sound perfectly fluent and similar to correct ones

Instead: evaluate behavior, not wording. The pass criteria should ask "did this do the right thing?" not "did this say the right words?"

**Bad criteria (wording-based):**
```
"Response contains the phrase 'I called on your behalf'"
```

**Good criteria (behavior-based):**
```
"Response shows Asmi made a phone call and returned concrete information from it.
Does not ask the user to call themselves. Does not invent call outcomes."
```

---

### 11.9 Traces are your debugging tool (Hamel & Shreya)

> *"A trace is the complete record of all actions, messages, tool calls, and data retrievals from a single user query to final response."*

For Asmi, a trace = the full chain: iMessage received → routing decision → tool (web/call/email) → response sent.

**Where to find Asmi's traces:**
- iMessage thread: the user-facing portion
- ElevenLabs dashboard: the call execution portion
- Internal logs (ask Satwik/Sibi for access): the routing and tool execution portion

When a test fails, pull all three. The iMessage response alone won't tell you where in the chain things broke.

---

### 11.10 The 30-minute weekly eval habit (Hamel & Shreya)

> *"After initial setup: 30 minutes per week. That's all it takes to keep evals healthy."*

**Weekly routine:**
1. Open `results_*.json` from the last run — scan for FAILs and UNCLEARs (5 min)
2. Read the raw responses for any failed tests — decide if the judge was right (10 min)
3. Read this week's `#internal-feedback` messages — anything that's not a test case yet? (10 min)
4. Add any missing test cases to `test_cases.py` (5 min)
5. Run: `python run_eval.py --category [the category you just updated]` to confirm they work

Total: 30 min. Do it every Monday before standup.

---

## Changelog of This Guide

| Date | Change |
|---|---|
| 2026-05-04 | Initial version — covers v0.10.37.2 regression suite, 28 tests across 8 categories |
| 2026-05-04 | Added Section 11: Industry best practices from Ankit Shukla (HelloPM), Hamel Husain & Shreya Shankar applied to Asmi |

> **Update this table every time you add tests or change the system.**
