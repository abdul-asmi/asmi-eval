# ─── Slack Audio & PDF Dispatcher ────────────────────────────────────────────────
# Sends call recordings and PDF evaluation reports to Slack using Slack Web API.
# Requires SLACK_BOT_TOKEN (starts with xoxb-) and SLACK_CHANNEL.

import os
import sys
import requests

def upload_file_to_slack(filepath: str, title: str, initial_comment: str = "", channel_id: str = None) -> bool:
    """
    Upload a local file to Slack using the modern 3-step file upload API.
    Zero external dependencies besides standard `requests` library.
    """
    token = os.environ.get("SLACK_BOT_TOKEN", "").strip()
    target_channel = (channel_id or os.environ.get("SLACK_CHANNEL", "")).strip()

    if not token or not target_channel:
        print("  [Slack] Skipping upload: SLACK_BOT_TOKEN or target channel not set.")
        return False

    if not filepath or not os.path.exists(filepath):
        print(f"  [Slack] File not found: {filepath}")
        return False

    filename = os.path.basename(filepath)
    file_size = os.path.getsize(filepath)

    try:
        # Step 1: Get upload URL
        res = requests.post(
            "https://slack.com/api/files.getUploadURLExternal",
            headers={"Authorization": f"Bearer {token}"},
            data={"filename": filename, "length": file_size},
            timeout=15,
        )
        res.raise_for_status()
        res_data = res.json()
        if not res_data.get("ok"):
            print(f"  [Slack] getUploadURLExternal failed: {res_data}")
            return False

        upload_url = res_data["upload_url"]
        file_id = res_data["file_id"]

        # Step 2: Upload file bytes
        with open(filepath, "rb") as f:
            upload_res = requests.post(upload_url, files={"file": f}, timeout=45)
        if upload_res.status_code != 200:
            print(f"  [Slack] Binary upload failed: HTTP {upload_res.status_code}")
            return False

        # Step 3: Complete upload and post to channel
        complete_res = requests.post(
            "https://slack.com/api/files.completeUploadExternal",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "files": [{"id": file_id, "title": title}],
                "channel_id": target_channel,
                "initial_comment": initial_comment,
            },
            timeout=15,
        )
        complete_res.raise_for_status()
        complete_data = complete_res.json()
        if not complete_data.get("ok"):
            print(f"  [Slack] completeUploadExternal failed: {complete_data}")
            return False

        print(f"  [Slack] Uploaded {filename} to channel {target_channel}")
        return True
    except Exception as e:
        print(f"  [Slack] Failed to upload {filename}: {e}")
        return False


def send_call_to_slack(
    conversation_id: str,
    test_id: str,
    test_name: str,
    call_transcript_result: dict | None = None,
    channel_id: str = None
) -> dict:
    """
    Download the call audio recording, generate the analysis PDF,
    and upload both files directly to your Slack channel.
    """
    state = {"audio_uploaded": False, "pdf_uploaded": False, "error": ""}
    
    from config import REPORTS_DIR, CALL_EVAL_PHONE
    from elevenlabs_phone import get_conversation, _parse_conversation, get_conversation_audio
    from report import generate_call_analysis_pdf

    os.makedirs(REPORTS_DIR, exist_ok=True)

    # 1. Download or locate audio file
    recording_path = os.path.join(REPORTS_DIR, f"recording_{conversation_id}.mp3")
    if not os.path.exists(recording_path):
        try:
            audio_bytes, _ = get_conversation_audio(conversation_id)
            with open(recording_path, "wb") as f:
                f.write(audio_bytes)
            print(f"  [Slack] Downloaded audio for {conversation_id}")
        except Exception as e:
            state["error"] = f"Audio download fail: {e}"
            print(f"  [Slack] Failed to download audio: {e}")
            recording_path = None

    # 2. Compile or locate PDF report
    pdf_path = os.path.join(REPORTS_DIR, f"analysis_{conversation_id}.pdf")
    if not os.path.exists(pdf_path):
        try:
            if not call_transcript_result:
                detail = get_conversation(conversation_id)
                call_transcript_result = _parse_conversation(detail)
            generate_call_analysis_pdf(
                call_data=call_transcript_result,
                test_id=test_id,
                test_name=test_name,
                call_phone=CALL_EVAL_PHONE,
                output_path=pdf_path,
            )
            print(f"  [Slack] Generated PDF for {conversation_id}")
        except Exception as e:
            state["error"] = (state["error"] or "") + f" PDF compilation fail: {e}"
            print(f"  [Slack] Failed to generate PDF: {e}")
            pdf_path = None

    # 3. Upload audio to Slack
    if recording_path and os.path.exists(recording_path):
        comment = f"📞 *Call Audio Recording*\n*Test:* {test_id} - {test_name}\n*Conversation ID:* `{conversation_id}`"
        title = f"Call Audio - {test_id} ({conversation_id[:8]})"
        state["audio_uploaded"] = upload_file_to_slack(recording_path, title=title, initial_comment=comment, channel_id=channel_id)

    # 4. Upload PDF to Slack
    if pdf_path and os.path.exists(pdf_path):
        comment = f"📄 *Call Evaluation Report*\n*Test:* {test_id} - {test_name}\n*Conversation ID:* `{conversation_id}`"
        title = f"Call Report - {test_id} ({conversation_id[:8]})"
        state["pdf_uploaded"] = upload_file_to_slack(pdf_path, title=title, initial_comment=comment, channel_id=channel_id)

    return state


def seed_slack_recordings(limit: int = 25) -> int:
    """
    Backfill/seed all recent ElevenLabs call recordings and PDFs to Slack.
    """
    token = os.environ.get("SLACK_BOT_TOKEN", "").strip()
    channel_id = os.environ.get("SLACK_CHANNEL", "").strip()
    if not token or not channel_id:
        print("  [Slack Seeding] Error: SLACK_BOT_TOKEN or SLACK_CHANNEL env vars are not set.")
        return 0

    from elevenlabs_phone import _agent_id, list_recent_conversations, _parse_conversation, get_conversation
    agent_id = _agent_id()
    if not agent_id:
        print("  [Slack Seeding] Error: ELEVENLABS_AGENT_ID is not configured.")
        return 0

    print(f"  [Slack Seeding] Querying last {limit} conversations from ElevenLabs (agent={agent_id})…")
    try:
        convos = list_recent_conversations(agent_id, limit=limit)
    except Exception as e:
        print(f"  [Slack Seeding] Failed to list conversations: {e}")
        return 0

    if not convos:
        print("  [Slack Seeding] No conversations found on ElevenLabs.")
        return 0

    print(f"  [Slack Seeding] Found {len(convos)} conversations. Starting backfill…")
    success_count = 0
    # Process from oldest to newest for chronological seeding
    for convo in reversed(convos):
        convo_id = convo.get("conversation_id") or convo.get("id")
        if not convo_id:
            continue
        print(f"\n  [Slack Seeding] Processing conversation: {convo_id}")
        try:
            detail = get_conversation(convo_id)
            parsed = _parse_conversation(detail)
            
            # Formulate hypothetical test details for backfill
            test_id = "backfill"
            test_name = f"Call eval backfill ({parsed.get('status') or 'unknown'})"
            
            state = send_call_to_slack(
                conversation_id=convo_id,
                test_id=test_id,
                test_name=test_name,
                call_transcript_result=parsed,
            )
            if state["audio_uploaded"] or state["pdf_uploaded"]:
                success_count += 1
        except Exception as e:
            print(f"  [Slack Seeding] Error processing {convo_id}: {e}")

    print(f"\n  [Slack Seeding] Backfill completed. Successfully seeded {success_count} conversations to Slack.")
    return success_count


def post_message_to_slack(text: str, channel_id: str = None) -> bool:
    """
    Post a plain text message to the Slack channel.
    """
    token = os.environ.get("SLACK_BOT_TOKEN", "").strip()
    target_channel = (channel_id or os.environ.get("SLACK_CHANNEL", "")).strip()

    if not token or not target_channel:
        return False

    try:
        res = requests.post(
            "https://slack.com/api/chat.postMessage",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json={"channel": target_channel, "text": text},
            timeout=15,
        )
        res.raise_for_status()
        data = res.json()
        return bool(data.get("ok"))
    except Exception as e:
        print(f"  [Slack] Failed to post message: {e}")
        return False


def _is_slack_command(text: str) -> bool:
    """Check if a message looks like a command."""
    t = text.strip().lower()
    if t.startswith("!"):
        return True
    keywords = ["run ", "rejudge", "status", "list", "help", "add test"]
    return any(t.startswith(k) for k in keywords)


def get_latest_slack_ts(channel_id: str = None) -> str | None:
    """
    Get the timestamp of the latest message in the Slack channel.
    Useful for initializing polling to ignore older history.
    """
    token = os.environ.get("SLACK_BOT_TOKEN", "").strip()
    target_channel = (channel_id or os.environ.get("SLACK_CHANNEL", "")).strip()

    if not token or not target_channel:
        return None

    try:
        res = requests.get(
            "https://slack.com/api/conversations.history",
            headers={"Authorization": f"Bearer {token}"},
            params={"channel": target_channel, "limit": 1},
            timeout=15,
        )
        res.raise_for_status()
        data = res.json()
        if data.get("ok") and data.get("messages"):
            return data["messages"][0].get("ts")
    except Exception as e:
        print(f"  [Slack] Failed to get latest ts: {e}")
    return None


def poll_slack_commands(last_ts: str | None) -> tuple[list[dict], str | None]:
    """
    Poll the Slack channel for new command messages sent after last_ts.
    Returns (list of command_dicts, updated_last_ts).
    Each command_dict contains: {"text": str, "ts": str}
    """
    token = os.environ.get("SLACK_BOT_TOKEN", "").strip()
    channel_id = os.environ.get("SLACK_CHANNEL", "").strip()

    if not token or not channel_id:
        return [], last_ts

    try:
        res = requests.get(
            "https://slack.com/api/conversations.history",
            headers={"Authorization": f"Bearer {token}"},
            params={"channel": channel_id, "limit": 10},
            timeout=15,
        )
        res.raise_for_status()
        data = res.json()
        if not data.get("ok"):
            print(f"  [Slack Polling] history failed: {data}")
            return [], last_ts

        messages = data.get("messages") or []
        if not messages:
            return [], last_ts

        # Sort messages from oldest to newest (by ts)
        messages = sorted(messages, key=lambda m: float(m.get("ts") or 0))

        new_commands = []
        current_last_ts = last_ts

        for msg in messages:
            msg_ts = msg.get("ts")
            if not msg_ts:
                continue

            # Update highest ts seen
            if not current_last_ts or float(msg_ts) > float(current_last_ts):
                current_last_ts = msg_ts

            # If this is not newer than our last processed ts, skip
            if last_ts and float(msg_ts) <= float(last_ts):
                continue

            # Ignore bot messages to avoid feedback loops
            if msg.get("bot_id") or msg.get("subtype") or "bot" in msg.get("user", "").lower():
                continue

            text = msg.get("text", "").strip()
            if not text:
                continue

            if _is_slack_command(text):
                new_commands.append({"text": text, "ts": msg_ts})

        return new_commands, current_last_ts

    except Exception as e:
        print(f"  [Slack Polling] Error polling commands: {e}")
        return [], last_ts


def get_bot_channels() -> list[str]:
    """
    Get the list of all channel IDs the bot is a member of.
    """
    token = os.environ.get("SLACK_BOT_TOKEN", "").strip()
    if not token:
        return []

    channels = []
    try:
        res = requests.get(
            "https://slack.com/api/users.conversations",
            headers={"Authorization": f"Bearer {token}"},
            params={"types": "public_channel,private_channel", "limit": 100},
            timeout=15,
        )
        res.raise_for_status()
        data = res.json()
        if data.get("ok"):
            for ch in data.get("channels", []):
                ch_id = ch.get("id")
                if ch_id:
                    channels.append(ch_id)
    except Exception as e:
        print(f"  [Slack] Failed to list bot channels: {e}")
    
    # Always include the default SLACK_CHANNEL if configured and not already present
    default_channel = os.environ.get("SLACK_CHANNEL", "").strip()
    if default_channel and default_channel not in channels:
        channels.append(default_channel)
        
    return channels


def poll_slack_commands_multi(channel_last_ts: dict[str, str]) -> tuple[list[dict], dict[str, str]]:
    """
    Poll all joined Slack channels for new command messages sent after their respective last_ts.
    channel_last_ts: dict mapping channel_id -> last_ts string.
    Returns (list of command_dicts, updated dict of channel_last_ts).
    Each command_dict contains: {"text": str, "ts": str, "channel_id": str}
    """
    token = os.environ.get("SLACK_BOT_TOKEN", "").strip()
    if not token:
        return [], channel_last_ts

    new_commands = []
    updated_ts_dict = dict(channel_last_ts)

    for channel_id in list(channel_last_ts.keys()):
        last_ts = channel_last_ts[channel_id]
        try:
            res = requests.get(
                "https://slack.com/api/conversations.history",
                headers={"Authorization": f"Bearer {token}"},
                params={"channel": channel_id, "limit": 10},
                timeout=15,
            )
            res.raise_for_status()
            data = res.json()
            if not data.get("ok"):
                continue

            messages = data.get("messages") or []
            if not messages:
                continue

            # Sort messages from oldest to newest (by ts)
            messages = sorted(messages, key=lambda m: float(m.get("ts") or 0))

            for msg in messages:
                msg_ts = msg.get("ts")
                if not msg_ts:
                    continue

                # Update highest ts seen for this channel
                if not updated_ts_dict.get(channel_id) or float(msg_ts) > float(updated_ts_dict[channel_id]):
                    updated_ts_dict[channel_id] = msg_ts

                # If this is not newer than our last processed ts, skip
                if last_ts and float(msg_ts) <= float(last_ts):
                    continue

                # Ignore bot messages to avoid feedback loops
                if msg.get("bot_id") or msg.get("subtype") or "bot" in msg.get("user", "").lower():
                    continue

                text = msg.get("text", "").strip()
                if not text:
                    continue

                if _is_slack_command(text):
                    new_commands.append({"text": text, "ts": msg_ts, "channel_id": channel_id})

        except Exception as e:
            print(f"  [Slack Polling] Error polling channel {channel_id}: {e}")

    return new_commands, updated_ts_dict


if __name__ == "__main__":
    # Add project root to sys.path so we can run directly
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

    if "--seed" in sys.argv:
        limit = 25
        for arg in sys.argv:
            if arg.startswith("--limit="):
                try:
                    limit = int(arg.split("=")[1])
                except ValueError:
                    pass
        seed_slack_recordings(limit=limit)
    else:
        print("Usage: python3 src/slack.py --seed [--limit=N]")
