# MECE Test Coverage Framework for Asmi Launch

**MECE** = **M**utually **E**xclusive, **C**ollectively **E**xhaustive

This framework ensures your test suite covers all angles without overlap, leaving no blind spots.

---

## 🎯 The Four Dimensions of Coverage

### Dimension 1: USER JOURNEY (Temporal)
Tests are organized by where users are in their lifecycle:

```
Pre-Onboarding → Onboarding → Post-Onboarding → Retention
     (0-5 min)     (5-20 min)     (20min-30days)   (30+ days)
```

| Stage | What's Being Tested | Example Tests |
|-------|---------------------|---------------|
| **Pre-Onboarding** | First impressions, call vs text preference | preonb_01 (YES), preonb_02 (NO), preonb_03 (text-only) |
| **Onboarding** | Core flow, capability understanding | All first-use patterns |
| **Post-Onboarding** | Task execution, personalization ramp | postonb_01 (task context), chat_01 (brevity) |
| **Retention** | Dormant user reactivation, utility updates | reengage_01 (off-topic link), reengage_02 (24hr nudge) |

**Why MECE:** Each user experiences each stage exactly once. Tests don't overlap across stages.

---

### Dimension 2: CHANNELS (Infrastructure)
Tests account for different communication channels with different latencies/behaviors:

```
iMessage (Apple) ←→ WhatsApp (Multi-platform)
```

| Channel | Characteristics | Tests |
|---------|-----------------|-------|
| **iMessage** | Fast, Apple-only, rich formatting | All core tests (primary channel) |
| **WhatsApp** | Slower latency, multi-platform, simpler formatting | missing_03 (latency), reduced formatting tests |
| **All-Channels** | Flows that work on both | Most onboarding, personalization |

**Why MECE:** Each message takes one channel. WhatsApp subset tests latency-sensitive flows.

---

### Dimension 3: FEATURES (Functional)
Tests are organized by what Asmi can DO:

```
Calling ← Chat → Task Management
   ↓       ↓          ↓
Voicemail Brevity  Checklist
```

| Feature | Core Capability | Tests | Gap Tests |
|---------|-----------------|-------|-----------|
| **Calling** | Make 3P calls, handle results | postonb_01, voicemail_01/02, summary_05 | missing_01 (dedup), missing_02 (multi-iter) |
| **Chat** | Respond to user messages naturally | chat_01/02/03, reengage_01 | missing_03 (WhatsApp latency) |
| **Task Management** | Create, track, manage tasks | checklist_01/02/03, personalize_02 | missing_08 (multi-user) |
| **Personalization** | Learn and adapt to user | personalize_01/02, reengage_03 | missing_04/05/06 (incentives, referrals, language) |
| **Guardrails** | Know what NOT to do | edge_01/02 | missing_07 (timezone) |

**Why MECE:** Each message/task uses one or more features. Tests verify each feature independently.

---

### Dimension 4: RISK LEVELS (Severity + Type)
Tests are categorized by what goes wrong and how critical:

```
P0 (Critical) ─→ P1 (Important) ─→ Nice-to-Have
```

| Risk Level | Shipping Impact | Examples | Count |
|-----------|-----------------|----------|-------|
| **P0: Critical** | Must pass before launch | Double-ask regression, no hallucination, text-only flow | 20 |
| **P1: Important** | Ship with known gaps if necessary | 24hr nudge, multi-iteration calls, WhatsApp latency | 7 |
| **Missing/Gaps** | Post-launch or feedback-driven | Feedback incentives, referral codes, language pref | 8 |

**Why MECE:** Each test has one priority. No test is both P0 and P1.

---

## 🧩 Additional MECE Dimensions (Sub-categories)

### MECE Angle: Test Type (How It's Tested)
Ensures different testing methodologies cover different aspects:

```
User Path    Edge Case    Regression    Load
(Happy path) (Odd inputs) (Known bugs)  (Stress)
```

| Type | Purpose | Examples |
|------|---------|----------|
| **User Path** | Happy path, expected flow | preonb_01 (YES path), preonb_03/04 (NO path) |
| **Edge Case** | Unusual but possible scenarios | voicemail_02 (no answer), edge_02 (missing info) |
| **Regression** | Known bugs that must not return | preonb_02 (🔴 double ask), summary_05 (hallucination) |
| **Load** | Multiple requests at once | postonb_02 (multiple calls), chat_02 (burst) |
| **Dedup** | Duplicate handling | missing_01 (call dedup) |
| **Integration** | Cross-system flows | missing_08 (multi-user scenarios) |

---

### MECE Angle: Quality Dimensions (What It Measures)
Ensures different quality aspects are tested:

```
Accuracy    Speed    UX       Safety
(Correct)  (Fast)  (Smooth)  (Safe)
```

| Quality | What | Examples |
|---------|------|----------|
| **Accuracy** | Right answer, no hallucination | summary_05, summary_06, edge_02 |
| **Speed** | Response time, latency | missing_03 (WhatsApp latency) |
| **UX** | User experience, nudging, framing | nudge_04/05/06 (3P call framing) |
| **Safety** | No unintended actions | edge_01 (no auto email/calendar), guardrails |
| **Completeness** | All parts addressed | summary_06 (multi-part questions) |

---

## 📊 Coverage Matrix: What's Covered vs. Missing

### COVERED (28 tests)

| Journey | Channel | Feature | Priority | Count |
|---------|---------|---------|----------|-------|
| Pre-Onboarding | iMessage + WhatsApp | Calling, Chat | P0 | 4 |
| Post-Onboarding | iMessage | Calling (5 tests) | P0 | 5 |
| Post-Onboarding | iMessage + WhatsApp | Chat (3) | P0 | 3 |
| Post-Onboarding | iMessage + WhatsApp | Task Mgmt (3) | P0 | 3 |
| Post-Onboarding | iMessage + WhatsApp | Personalization (2) | P0 | 2 |
| Post-Onboarding | iMessage | 3P Nudging (3) | P0 | 3 |
| Retention | iMessage + WhatsApp | Engagement, Reactivation | P0/P1 | 3 |
| Post-Onboarding | iMessage + WhatsApp | Guardrails (2) | P0 | 2 |

**Total Covered: 28 tests (77%)**

---

### MISSING / GAPS (8 tests identified)

| Gap | Stage | Feature | Priority | Why Important | Fix Strategy |
|-----|-------|---------|----------|----------------|--------------|
| **Call dedup for burst** | Post-Onboarding | Calling | P0 | Rapid fire calls should only trigger once | Add `type: 'dedup'` test |
| **Multi-iteration calls** | Post-Onboarding | Calling | P1 | Real tasks need multiple calls | Add sequence test with 3+ calls |
| **WhatsApp latency** | Post-Onboarding | Chat | P1 | WhatsApp is slower; need baseline | Add latency measurement test |
| **Feedback loop + incentives** | Retention | Engagement | P0 | Users need reason to give feedback | Create feedback flow test |
| **Referral code system** | Retention | Engagement | P0 | Exclusivity & growth require referrals | Test signup with code |
| **Language preference** | Onboarding | Personalization | P0 | Non-English users must work | Add lang selection test |
| **Timezone awareness** | Post-Onboarding | Calling | P0 | Times must be correct for user | Add timezone context test |
| **Multi-user scenarios** | Post-Onboarding | Chat | P1 | Shared accounts or family use | Test 2+ users on same account |

---

## 🎯 Launch Readiness Checklist

### MUST PASS (P0 - All 20 tests):
- [ ] ✅ preonb_01 - First call YES path
- [ ] 🔴 **preonb_02 - NO DOUBLE ASK** ← CRITICAL REGRESSION
- [ ] ✅ preonb_03 - NO → text-only mode
- [ ] ✅ preonb_04 - NO → smart task nudge
- [ ] ✅ postonb_01 - Task-specific call context
- [ ] ✅ postonb_02 - Multiple calls in sequence
- [ ] ✅ voicemail_01 - Voicemail left & reported
- [ ] ✅ voicemail_02 - No voicemail = honest
- [ ] ✅ summary_05 - Call summary no hallucination
- [ ] ✅ summary_06 - Multi-part call handling
- [ ] ✅ chat_01 - Chat brevity 1-2 sentences
- [ ] ✅ chat_02 - No message barrage
- [ ] ✅ chat_03 - Chat ends cleanly
- [ ] ✅ checklist_01 - Create from task
- [ ] ✅ checklist_02 - Track completion
- [ ] ✅ checklist_03 - Readable format
- [ ] ✅ personalize_01 - Learn user info
- [ ] ✅ personalize_02 - Daily catchup personalizes
- [ ] ✅ nudge_04 - 3P first call framing
- [ ] ✅ nudge_05/06 - 3P consistent framing
- [ ] ✅ reengage_01 - Off-topic linked to task
- [ ] ✅ edge_01 - No email/calendar auto-trigger
- [ ] ✅ edge_02 - No hallucination on missing info

### SHOULD PASS (P1 - Best Effort):
- [ ] ⏳ reengage_02 - 24hr nudge system
- [ ] ⏳ reengage_03 - Utility 2.0 from chat

### KNOWN GAPS (Post-Launch):
- [ ] ❌ missing_01 - Call dedup for burst
- [ ] ❌ missing_02 - Multi-iteration calls
- [ ] ❌ missing_03 - WhatsApp latency test
- [ ] ❌ missing_04 - Feedback loop incentives
- [ ] ❌ missing_05 - Referral code system
- [ ] ❌ missing_06 - Language preference
- [ ] ❌ missing_07 - Timezone awareness
- [ ] ❌ missing_08 - Multi-user scenarios

---

## 🔍 How to Read the Prism & Knowledge Graph

### 3D Knowledge Graph View:
- **Nodes** = Test cases
  - 🔴 Red = P0 (Critical)
  - 🟡 Amber = P1 (Important)
  - 🔵 Blue = Missing/Gap
  - 🟢 Green = Done
- **Edges** = Relationships
  - Shared features, same priority, same channel
  - Thicker edges = stronger relationship
- **Position** = Test context
  - X/Y = Similarity in test type
  - Z = Priority level

### Filters (Left Sidebar):
- **User Journey**: See tests at each lifecycle stage
- **Channels**: Filter iMessage vs WhatsApp vs both
- **Priority**: Show P0 only (for launch) or include P1
- **MECE Coverage**: View gaps organized by dimension

### Interactive:
- Hover node = highlight connections
- Click node = see test details
- Rotate graph = explore relationships

---

## 📝 New Test Case Categories (Not Existing)

These categories were added to improve MECE coverage:

### New Categories:

1. **voicemail** (Previously under call_summary)
   - Voicemail handling is distinct from call success
   - Separate P0s for "left message" vs "no answer"

2. **task_specific_call** (New)
   - Calls that need task context
   - Different from generic calling

3. **chat_brevity** (Split from chat_experience)
   - Message length is its own concern
   - Affects UX directly

4. **reengagement** (Split from retention)
   - Dormant user patterns
   - 24hr nudges
   - Utility updates
   - Distinct from feedback loops

5. **threep_nudge** (Standalone)
   - 3P call framing consistency
   - First vs subsequent call messaging
   - Was scattered, now consolidated

6. **guardrails** (New)
   - Safety tests
   - What Asmi must NOT do
   - Email/calendar auto-trigger prevention
   - Hallucination prevention

7. **timezone** (New)
   - Timezone detection from location
   - Timezone-aware call times
   - Distinct from location_memory

8. **language_pref** (New)
   - Language selection in calls
   - No re-asking for language
   - Distinct from personalization

---

## 🚀 Using This Framework

### For Planning New Tests:
1. **Pick a user journey stage** (pre-onboarding → retention)
2. **Pick a channel** (iMessage, WhatsApp, or both)
3. **Pick a feature** (calling, chat, task-mgmt, etc.)
4. **Pick a risk level** (P0, P1, or gap)
5. **Pick a test type** (user-path, edge-case, regression, load)

Example: "I want to test WhatsApp chat brevity in post-onboarding for P0"
→ Test goes in **chat_experience** category, **post-onboarding** stage, **whatsapp** channel, **p0** priority

### For Finding Gaps:
Look at the matrix — if a cell is empty, that's a coverage gap:
- No P0 tests for WhatsApp chat? → Gap
- No retention tests on iMessage? → Gap
- No edge-case tests for calling? → Gap

### For Prioritization:
- **P0 tests in "pre-onboarding" stage** = Launch blockers
- **P0 tests in "post-onboarding" stage** = Feature completeness
- **P1 tests** = Nice to have, but ship if blocked
- **Missing tests** = Post-launch roadmap

---

## 📚 Files

- **Test Cases**: `test_cases_launch.py` (47 tests)
- **Prism Graph**: `test_prism_graph.html` (3D interactive visualization)
- **MECE Framework**: This file
- **Configuration Guide**: `TEST_CASE_GUIDE.md`

---

## 🎓 Quick Reference: MECE Principles

**What makes something MECE?**
- ✅ No overlap (Mutually Exclusive) — each item belongs in exactly one category
- ✅ Complete coverage (Collectively Exhaustive) — nothing falls through cracks
- ❌ Fuzzy categories — leads to double-testing or missed coverage
- ❌ Single-axis thinking — misses compound risks

**Example of MECE broken:**
- Categories: "P0 tests", "Chat tests", "Missing tests"
- Problem: A P0 chat test could be P0 or Chat or Missing → overlaps!

**Example of MECE fixed:**
- Axes: USER JOURNEY (stage) + FEATURE (what) + PRIORITY (severity) + RISK (type)
- Each test = one stage × one feature × one priority × one type → No overlap, complete coverage

---

**The Prism Graph lets you explore all these dimensions interactively.** Hover to see relationships. Filter to focus on one dimension. Click nodes to inspect details. Use this to spot gaps and plan post-launch improvements.
