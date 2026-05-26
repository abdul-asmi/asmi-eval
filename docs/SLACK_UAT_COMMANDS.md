# Slack PM UAT Commands

## Current Status

Slack command control is connected.

- Workspace: Asmi AI
- Bot: testing_calls_11labs
- Listening channel: `#call-testing-11labs`
- Channel ID: `C0B54EYTW31`
- Bot DMs: supported when the Mac daemon is running
- Mac daemon: required and currently the executor for iMessage/chat.db tests
- Hosted UI source of truth: Render/Supabase test definitions

The Slack bot listens for `!uat ...` commands in channels it is a member of and in bot DMs. Final UAT reports go to the command sender's Slack DM when Slack can open the DM. If DM delivery fails, the summary falls back to the command channel.

## Commands

### `!uat ping`

Verifies the Slack -> Mac daemon command path without starting a test run.

Expected response:

```text
UAT ping OK.
Slack channel: ...
Slack user captured for DM routing: yes
Mac daemon: running this command handler now.
```

Use this first if you are unsure whether Slack control is connected.

### `!uat config`

Shows the active UAT configuration.

Includes:

- Target environment and Asmi number
- Slack channel ID
- Loaded test count
- PM core IDs
- Which PM core IDs are available or missing

### `!uat core`

Runs the PM-approved core suite only.

Current core set:

- `core_02v1`
- `core_03v1`
- `core_04v1`
- `core_05v1`

Rules:

- Does not run onboarding.
- Does not run `core_01v1`.
- Does not expand core unless the PM explicitly changes the core list in code later.
- Loads the latest synced UI definitions by test ID.

Use this when you want a quick product answer to: "Are the current core flows working?"

### `!uat interactive`

Starts an open-ended break-finding run.

Behavior:

- The runner sends one natural message to Asmi at a time.
- It reads Asmi's reply from Messages/chat.db.
- It chooses the next best stress-test reply automatically.
- It also watches for any message you manually send to Asmi from the Messages app.
- If you manually intervene, your message becomes part of the transcript and the runner branches from that state.
- It keeps going until you send `!uat stop`.

Use this when you want to explore generally and find where Asmi breaks.

### `!uat stop`

Stops an active open-ended `!uat interactive` run.

Behavior:

- Stops sending new messages.
- Gathers transcript/evidence.
- Evaluates the run.
- Sends the PM summary and artifacts to Slack.

### `!uat interactive <thing>`

Runs focused interactive stress testing for one area.

Examples:

- `!uat interactive 3p calls`
- `!uat interactive pdf`
- `!uat interactive task state`
- `!uat interactive guardrails positive`
- `!uat interactive adhoc calls`

Focused runs may stop automatically when there is enough evidence for pass/fail, unlike open-ended `!uat interactive`.

### `!uat changelog <release notes>`

Generates new interactive test scenarios from the pasted changelog and runs them.

Important behavior:

- Existing tests are reference only.
- The runner creates new changelog-specific interactive scenarios.
- It prioritizes calls, 3P execution, PDFs, task state, guardrails, routing, and messaging behavior.

Example:

```text
!uat changelog
v0.11.0.6.1
- Fixed 3P queued/scheduled false confirmations
- Improved PDF generation confirmation
- Guardrails now allow personal crisis messages to family
```

### `!uat status`

Shows the current run state.

Includes:

- Active run type
- Focus area
- Elapsed time
- Turn count
- Last runner state

### `!uat last`

Re-sends the latest PM report and available artifacts.

Use this when Slack upload failed, a teammate asks for the latest run, or you want the bundle again.

## Reporting Format

Reports are written for PM/product communication:

```text
Tested with N scenarios on <area>: <plain product conclusion>.

What passed
- ...

What failed or needs attention or could be improved
- ...

Artifacts
- ...

Ship signal
- ...

Top fix before next build
- ...
```

## Artifact Bundle

When available, Slack uploads:

- Full run PDF
- Raw results JSON
- Interactive transcript JSON/PDF
- Call recordings
- Call analysis PDFs
- ElevenLabs conversation references when present in the run output

## Interactive Test Rules

The runner must keep messages natural.

Do not send internal test words to Asmi:

- `dummy`
- `probe`
- `eval`
- `guardrail test`

Use natural details instead:

```text
Use (412) 555-0198 as my reservation contact number.
```

Product scoring rules:

- "Queued" or "scheduled" is not success unless a real call outcome follows.
- If Asmi gives vague status, ask whether the call connected and what happened.
- If Asmi context-bleeds, redirect once and mark context bleed.
- For open-ended `!uat interactive`, only `!uat stop` ends the run.
- For focused/changelog interactive runs, the runner may stop when enough evidence exists.

## How To Use It In Slack

1. Go to `#call-testing-11labs`.
2. Type `!uat ping` to verify Slack -> Mac daemon connectivity.
3. Type `!uat config` to confirm target and exact core IDs.
4. Type `!uat core` for the PM core run.
5. Type `!uat interactive` for open-ended break-finding.
6. If open-ended interactive is running, type `!uat stop` when you have enough signal.
7. Type `!uat last` to resend the last report/artifacts.

## Next Steps

Recommended immediate checks:

- Send `!uat ping` in `#call-testing-11labs` and confirm the bot replies.
- Send `!uat config` and confirm it shows all four core IDs available.
- Send `!uat core` and verify it runs exactly `core_02v1`, `core_03v1`, `core_04v1`, `core_05v1`.
- Confirm the final report lands in your Slack DM.
- Confirm PDF/JSON artifacts upload.
- Confirm call_eval recordings upload for `core_03v1` and `core_05v1` when calls complete.

Recommended product improvements:

- Add stronger artifact labels so Slack files say which command/run generated them.
- Add run IDs to every Slack report so PMs can reference a specific UAT pass.
- Add a manual "include my Messages-app intervention" marker in the report when the PM steps into an interactive run.
