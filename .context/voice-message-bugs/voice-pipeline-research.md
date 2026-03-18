# Voice Message Pipeline — Research

## Overview

Voice messages flow through a 4-stage pipeline: Telegram audio download → Groq/Whisper transcription → command routing (fast-path prefix match OR Claude intent classification) → action execution. The pipeline treats transcribed voice text identically to typed text, but voice transcriptions are inherently noisier — Whisper adds punctuation, filler words, and natural language phrasing that the text parsing pipeline doesn't handle.

## Key Files

| File | Role | Key Lines |
|------|------|-----------|
| `bot/handlers.py` | Voice handler entry point, process_input router | 231-250 (handle_voice), 44-75 (process_input) |
| `bot/commands.py` | Command routing, fast-path matching, intent dispatch, cmd_delete | 9-24 (_clean_num), 27-44 (_COMMAND_TABLE), 47-88 (route_command), 91-127 (_classify_and_dispatch), 130-174 (_dispatch_single_intent), 645-677 (cmd_delete) |
| `services/claude.py` | Intent classifier prompt and API call | 77-155 (classify_intent) |
| `services/groq.py` | Whisper transcription | 7-13 (transcribe_voice) |
| `bot/state.py` | In-memory state + task_map persistence | 1-63 |

## Architecture

```
Voice message (OGG)
  → handle_voice() [handlers.py:231]
    → transcribe_voice() [groq.py:7] — Groq Whisper Large V3
    → Reply: "I heard: {text}"
    → process_input(text, source="voice") [handlers.py:44]
      → route_command(text) [commands.py:47]
        → FAST PATH: prefix match against _COMMAND_TABLE [commands.py:62-85]
           → cmd_delete(args) → _clean_num(args) → task_map lookup → DB delete
        → SLOW PATH: _classify_and_dispatch() [commands.py:91]
           → classify_intent() [claude.py:77] — Claude Sonnet parses intent
           → _dispatch_single_intent() [commands.py:130]
              → cmd_delete(num) → task_map lookup → DB delete
      → Fallback: chat() [handlers.py:71]
```

## Data Flow

### Voice → Transcription
1. `handle_voice()` downloads OGG audio bytes from Telegram (handlers.py:236-237)
2. Sends bytes to `transcribe_voice()` which calls Groq API with Whisper Large V3 (groq.py:8-12)
3. Returns plain text, `.strip()` applied (groq.py:13)
4. Bot replies "I heard: {text}" to show transcription (handlers.py:248)
5. Passes text to `process_input()` with `source="voice"` (handlers.py:250)

### Command Routing
1. `process_input()` calls `route_command()` (handlers.py:56)
2. `route_command()` tries fast-path prefix match first (commands.py:65-85)
3. If no fast-path match, falls through to `_classify_and_dispatch()` (commands.py:88)
4. Claude classifies intent using the full task context (commands.py:91-127)
5. Dispatch calls the appropriate `cmd_*` handler (commands.py:130-174)

### Delete Execution
1. `cmd_delete(args)` calls `_clean_num(args)` to extract number (commands.py:652)
2. Looks up UUID via `state["task_map"][num]` (commands.py:653)
3. Queries PostgreSQL by UUID (commands.py:659)
4. Deletes from DB (commands.py:668) and archives in Notion (commands.py:672)
5. Removes from in-memory task_map (commands.py:675)

## Key Functions/Components

### `_clean_num(raw)` — commands.py:9-24
- **Purpose:** Strip filler words from number arguments
- **Input:** Raw string like "task 19", "number four", "one"
- **Output:** Cleaned number string like "19", "4", "1"
- **Logic:** Removes words ("task", "number", "#"), checks word-to-number map (one→1 through twenty→20)
- **Called by:** cmd_delete, cmd_done, cmd_doing, _set_status, cmd_edit

### `route_command(text)` — commands.py:47-88
- **Purpose:** Route text to appropriate handler
- **Input:** User text (typed or transcribed)
- **Output:** True if handled, False if should fall through to chat
- **Logic:** Fast-path prefix match → Claude classification fallback
- **Called by:** process_input (handlers.py:56), handle_slash_command (handlers.py:183)

### `classify_intent(text, projects, task_map, task_context, people, history)` — claude.py:77-155
- **Purpose:** Use Claude to parse natural language into structured intent JSON
- **Input:** User text + full context (projects, task map, open tasks, people, conversation history)
- **Output:** JSON dict or array of intents
- **Called by:** _classify_and_dispatch (commands.py:103)

### `transcribe_voice(audio_bytes)` — groq.py:7-13
- **Purpose:** Convert voice audio to text
- **Input:** Raw audio bytes (OGG from Telegram)
- **Output:** Plain text string
- **Called by:** handle_voice (handlers.py:242)

## Dependencies

- `handle_voice` → `transcribe_voice` → `process_input` → `route_command`
- `route_command` → `_clean_num` (fast path) OR `classify_intent` (slow path)
- `cmd_delete` → `_clean_num` → task_map lookup → DB query
- `classify_intent` requires: projects list, task_map, task_context string, people list, conversation history
- Task map must be populated before any numbered command works

## Identified Issues

### Issue 1: `_clean_num` does not strip punctuation (commands.py:9-24)

Whisper consistently adds punctuation to transcriptions (periods, commas, question marks). Examples:
- Voice: "delete one" → Whisper: "Delete one." → fast path args: "one." → `_clean_num("one.")` → removes "task"/"number" (no change) → checks word_nums for "one." → **NOT FOUND** → returns "one." → task_map.get("one.") → **None** → "No task #one."
- Voice: "delete 3" → Whisper: "Delete 3." → `_clean_num("3.")` → returns "3." → task_map.get("3.") → **None** → fails

The fix is trivial: strip non-alphanumeric characters in `_clean_num`.

### Issue 2: `_clean_num` doesn't handle common voice filler words (commands.py:9-24)

Only strips "task", "number", and "#". Voice transcriptions commonly include:
- Articles: "the" ("delete the first task")
- Politeness: "please" ("delete task one please")
- Demonstratives: "that" ("delete that task")
- Filler: "um", "uh"

Example: "delete 3 please" → `_clean_num("3 please")` → returns "3 please" → fails.

### Issue 3: No ordinal number support (commands.py:11-16)

Word-to-number map only has cardinal numbers (one, two, three). Voice users commonly say ordinals:
- "delete the first one" → `_clean_num("the first one")` → removes "task"/"number" → "the first one" → not in word_nums → fails
- "mark the third task as done" → similar failure

### Issue 4: Task map context in classifier is disconnected from task titles (claude.py:92-106)

The classify_intent prompt sends two separate, unlinked pieces of information:

**Task numbers (lines 92-96):**
```
Currently displayed task numbers:
  #1
  #2
  #3
```

**Task context (line 106):**
```
Current open tasks:
  - Buy groceries | status:not_started | priority:high | project:Home
  - Call dentist | status:not_started | priority:medium
```

Claude has NO WAY to know that #1 = "Buy groceries" and #2 = "Call dentist". When a user says "delete the grocery task" via voice, Claude can see the task exists but cannot determine its number to return `{"intent": "delete", "num": "1"}`.

This is the most impactful bug for voice UX. Voice users refer to tasks by description ("delete the grocery one"), not by number.

### Issue 5: Fast path is too aggressive for voice-transcribed text (commands.py:62-85)

The fast path matches any text starting with "delete " and sends the remainder directly to `_clean_num`. This works for typed commands but fails for voice because:

- "Delete that task about groceries" → fast path grabs it, args="that task about groceries" → `_clean_num` can't parse → fails with confusing error
- "Delete, um, task one" → fast path, args=", um, task one" → fails
- The text should have gone to Claude's classifier which could understand natural language

The compound detection (line 69: checking for "and"/newlines) is the only guard, but it doesn't catch single-intent natural language.

### Issue 6: No voice-specific handling anywhere in the pipeline (handlers.py:250)

`process_input()` receives `source="voice"` but never uses it. The parameter is passed but ignored:
```python
async def process_input(text: str, update: Update, source: str = "text"):
    # ... source is never checked ...
    from bot.commands import route_command
    handled = await route_command(text, update, state)  # source not forwarded
```

`route_command()` doesn't accept a source parameter. Voice and text are processed identically despite fundamentally different input characteristics (voice is noisier, more natural language, has punctuation artifacts).

### Issue 7: Whisper transcription has no post-processing (groq.py:7-13)

The transcription is returned with only `.strip()` applied. Common Whisper artifacts that should be cleaned:
- Trailing periods on short commands: "Delete one." → "Delete one"
- Capitalization: "DELETE ONE" (shouting) vs "delete one"
- Filler words: "Um, delete task one, please."

## Patterns & Conventions

- All command handlers follow signature `async def cmd_*(args: str, update: Update, state: dict)`
- Fast path → slow path (Claude) → chat fallback routing pattern
- Task resolution always goes through display number → UUID via task_map
- State is in-memory dict with DB persistence for task_map only
- Claude responses are JSON-parsed with code fence stripping
- Error messages follow pattern "No task #{num}. Use /list first."

## Testing

No automated tests exist in this codebase. Testing is manual via Telegram.

## Related Systems

- **Groq API** — Whisper transcription service
- **Anthropic API** — Claude intent classification (Sonnet 4.6)
- **Notion API** — Task sync and archival
- **PostgreSQL** — Primary data store
- **Telegram Bot API** — Message handling via python-telegram-bot

## Complexity Estimate

- **Files to modify:** 3 (`bot/commands.py`, `services/claude.py`, `bot/handlers.py`)
- **Estimated phases:** 1
- **Rationale:** All issues are in the same pipeline and closely related. Fixes are surgical: improve `_clean_num`, fix the classifier prompt, add voice-aware routing. No architectural changes needed.
- **Risk areas:** The classifier prompt change (Issue 4) needs careful formatting to avoid breaking existing text command classification. The fast-path bypass for voice (Issue 5+6) needs to preserve fast-path efficiency for typed commands.
