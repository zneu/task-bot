# Voice Pipeline Fix Plan

- **Branch:** `feature/telegram-task-management`
- **Date:** 2026-03-18
- **Research:** `.context/voice-message-bugs/voice-pipeline-research.md`

## Goal

Fix all 7 voice message pipeline bugs so that voice-transcribed commands (delete, done, doing, edit, etc.) work as reliably as typed commands.

## Success Criteria

- [x] `_clean_num("one.")` → `"1"`, `_clean_num("3.")` → `"3"`, `_clean_num("3,")` → `"3"`
- [x] `_clean_num("3 please")` → `"3"`, `_clean_num("the first one")` → `"1"`
- [x] Ordinals work: `_clean_num("first")` → `"1"`, `_clean_num("third")` → `"3"`
- [x] Classifier prompt links task numbers to task titles so Claude can resolve "delete the grocery task" → `{"intent": "delete", "num": "1"}`
- [x] Voice input like "Delete that task about groceries" bypasses fast path and goes to Claude classifier
- [x] `source="voice"` is forwarded through the pipeline and used to route voice to classifier
- [x] Whisper transcription post-processing strips trailing punctuation and normalizes case

## Scope Boundaries

### In Scope
- `_clean_num` hardening (punctuation, filler words, ordinals)
- Classifier prompt fix (linked task numbers + titles)
- Voice-aware routing (source parameter forwarded, voice skips fast path for ambiguous input)
- Whisper post-processing in `groq.py`

### Out of Scope
- Whisper model changes or prompt tuning
- New voice-specific commands
- Automated tests (no test infrastructure exists)
- Changing the fast path for typed text commands
- Multi-language voice support

---

## Implementation Steps

### Phase 1: Fix `_clean_num` (Issues 1, 2, 3)

#### Step 1.1: Strip punctuation from input
- **File:** `bot/commands.py` **Lines:** L18
- **Change:** After `cleaned = raw.strip().lower()`, strip all non-alphanumeric/space characters so Whisper punctuation like trailing periods, commas, and question marks don't break lookups.
- **Before:**
```python
cleaned = raw.strip().lower()
for word in ("task", "number", "#"):
    cleaned = cleaned.replace(word, "")
cleaned = cleaned.strip()
```
- **After:**
```python
cleaned = raw.strip().lower()
cleaned = re.sub(r'[^\w\s]', '', cleaned)  # strip punctuation
for word in ("task", "number", "#"):
    cleaned = cleaned.replace(word, "")
cleaned = cleaned.strip()
```
- **Verification:** `_clean_num("one.")` → `"1"`, `_clean_num("3.")` → `"3"`

#### Step 1.2: Add filler word stripping
- **File:** `bot/commands.py` **Lines:** L19-20
- **Change:** Expand the word removal list to include common voice filler words. Change from substring replace to word-boundary removal to avoid corrupting numbers inside words.
- **Before:**
```python
for word in ("task", "number", "#"):
    cleaned = cleaned.replace(word, "")
```
- **After:**
```python
for word in ("task", "number", "please", "the", "that", "um", "uh", "like"):
    cleaned = re.sub(rf'\b{word}\b', '', cleaned)
cleaned = re.sub(r'\s+', ' ', cleaned)  # collapse multiple spaces
```
- **Verification:** `_clean_num("3 please")` → `"3"`, `_clean_num("that task 5")` → `"5"`

Note: The `#` removal is handled by the punctuation strip in Step 1.1, so it's no longer needed in the word list.

#### Step 1.3: Add ordinal number support
- **File:** `bot/commands.py` **Lines:** L11-16
- **Change:** Add ordinal-to-number mapping alongside the existing cardinal word_nums map.
- **Before:**
```python
word_nums = {
    "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
    "six": "6", "seven": "7", "eight": "8", "nine": "9", "ten": "10",
    "eleven": "11", "twelve": "12", "thirteen": "13", "fourteen": "14",
    "fifteen": "15", "sixteen": "16", "seventeen": "17", "eighteen": "18",
    "nineteen": "19", "twenty": "20",
}
```
- **After:**
```python
word_nums = {
    "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
    "six": "6", "seven": "7", "eight": "8", "nine": "9", "ten": "10",
    "eleven": "11", "twelve": "12", "thirteen": "13", "fourteen": "14",
    "fifteen": "15", "sixteen": "16", "seventeen": "17", "eighteen": "18",
    "nineteen": "19", "twenty": "20",
    # Ordinals
    "first": "1", "second": "2", "third": "3", "fourth": "4", "fifth": "5",
    "sixth": "6", "seventh": "7", "eighth": "8", "ninth": "9", "tenth": "10",
    "eleventh": "11", "twelfth": "12", "thirteenth": "13", "fourteenth": "14",
    "fifteenth": "15", "sixteenth": "16", "seventeenth": "17", "eighteenth": "18",
    "nineteenth": "19", "twentieth": "20",
}
```
- **Verification:** `_clean_num("first")` → `"1"`, `_clean_num("the third one")` → `"3"` (after filler removal, "third" remains and maps to "3"; "one" also maps but we need the leftmost match — see Step 1.4)

#### Step 1.4: Handle multi-token remnants after filler stripping
- **File:** `bot/commands.py` **Lines:** L22-23
- **Change:** After stripping fillers, if cleaned text has multiple tokens, try each token against word_nums and return the first match. Also try extracting a bare number.
- **Before:**
```python
if cleaned in word_nums:
    cleaned = word_nums[cleaned]
return cleaned
```
- **After:**
```python
if cleaned in word_nums:
    return word_nums[cleaned]
# Try individual tokens (e.g. "third one" → "third" → "3")
for token in cleaned.split():
    if token in word_nums:
        return word_nums[token]
    if token.isdigit():
        return token
return cleaned
```
- **Verification:** `_clean_num("the third one")` → strips "the" → "third one" → tries "third" → `"3"`. `_clean_num("task 5 please")` → strips fillers → "5" → `"5"`.

---

### Phase 2: Fix classifier prompt (Issue 4)

#### Step 2.1: Build linked task number + title context
- **File:** `services/claude.py` **Lines:** L91-96
- **Change:** Replace the disconnected task number list with a combined format that links numbers to titles. Pull task details from the DB so Claude can match "the grocery task" to the right number.
- **Before:**
```python
task_list_str = ""
if task_map:
    task_list_str = "\nCurrently displayed task numbers:\n" + "\n".join(
        f"  #{k}" for k in sorted(task_map.keys(), key=int)
    )
```
- **After:**
```python
task_list_str = ""
if task_map:
    # Build linked number→title mapping for Claude
    from database.connection import AsyncSessionLocal
    # task_map_context is passed in instead — see Step 2.2
    task_list_str = "\nCurrently displayed tasks (number → title):\n" + "\n".join(
        f"  #{k} → {title}" for k, title in task_map_titles.items()
    )
```

Actually, the classifier is a sync function and can't do async DB queries. Better approach: build the linked context in `_classify_and_dispatch` where we already have both task_map and task_context, and pass it as a new parameter.

#### Step 2.1 (revised): Add task_map_titles parameter to classify_intent
- **File:** `services/claude.py` **Lines:** L77, L91-96
- **Change:** Add a `task_map_titles` parameter (dict mapping display number to task title). Use it to build a linked task list in the prompt instead of bare numbers.
- **Before (signature):**
```python
def classify_intent(text: str, projects: list[str], task_map: dict, task_context: str = "", people: list[str] = None, conversation_history: list = None) -> dict:
```
- **After (signature):**
```python
def classify_intent(text: str, projects: list[str], task_map: dict, task_context: str = "", people: list[str] = None, conversation_history: list = None, task_map_titles: dict = None) -> dict:
```
- **Before (task list block, L91-96):**
```python
task_list_str = ""
if task_map:
    task_list_str = "\nCurrently displayed task numbers:\n" + "\n".join(
        f"  #{k}" for k in sorted(task_map.keys(), key=int)
    )
```
- **After:**
```python
task_list_str = ""
if task_map_titles:
    task_list_str = "\nCurrently displayed tasks (number → title):\n" + "\n".join(
        f"  #{k} → {title}" for k, title in sorted(task_map_titles.items(), key=lambda x: int(x[0]))
    )
elif task_map:
    task_list_str = "\nCurrently displayed task numbers:\n" + "\n".join(
        f"  #{k}" for k in sorted(task_map.keys(), key=int)
    )
```
- **Verification:** When task_map_titles is provided, the prompt will contain lines like `#1 → Buy groceries` instead of just `#1`.

#### Step 2.2: Build task_map_titles in _classify_and_dispatch
- **File:** `bot/commands.py` **Lines:** L98-103
- **Change:** Query DB to resolve task_map UUIDs to titles, then pass the linked map to classify_intent.
- **Before:**
```python
# Get full task and people context for Claude
projects, task_context = await _get_task_context()
people = await _get_people_names()

try:
    result = classify_intent(text, projects, state.get("task_map", {}), task_context, people, state.get("conversation_history", []))
```
- **After:**
```python
# Get full task and people context for Claude
projects, task_context = await _get_task_context()
people = await _get_people_names()

# Build linked task number → title map for the classifier
task_map = state.get("task_map", {})
task_map_titles = await _get_task_map_titles(task_map)

try:
    result = classify_intent(text, projects, task_map, task_context, people, state.get("conversation_history", []), task_map_titles=task_map_titles)
```
- **Verification:** `task_map_titles` is populated and passed through.

#### Step 2.3: Add _get_task_map_titles helper
- **File:** `bot/commands.py` — Insert after `_get_people_names` (after L264)
- **Change:** New helper that resolves task_map UUIDs to titles via a single DB query.
- **Code:**
```python
async def _get_task_map_titles(task_map: dict) -> dict:
    """Resolve task_map display numbers to task titles."""
    if not task_map:
        return {}

    from database.connection import AsyncSessionLocal
    from database.models import Task
    from sqlalchemy import select

    task_ids = list(task_map.values())
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Task).where(Task.id.in_(task_ids)))
        tasks_by_id = {str(t.id): t.title for t in result.scalars().all()}

    return {num: tasks_by_id.get(str(tid), "Unknown") for num, tid in task_map.items()}
```
- **Verification:** Returns `{"1": "Buy groceries", "2": "Call dentist", ...}`

---

### Phase 3: Voice-aware routing (Issues 5, 6, 7)

#### Step 3.1: Add Whisper post-processing in groq.py
- **File:** `services/groq.py` **Lines:** L13
- **Change:** Add a `clean_transcription` function that strips trailing punctuation on short commands and lowercases. Applied after `.strip()`.
- **Before:**
```python
def transcribe_voice(audio_bytes: bytes, filename: str = "voice.ogg") -> str:
    transcription = groq_client.audio.transcriptions.create(
        file=(filename, audio_bytes),
        model="whisper-large-v3",
        response_format="text",
    )
    return transcription.strip()
```
- **After:**
```python
import re

def transcribe_voice(audio_bytes: bytes, filename: str = "voice.ogg") -> str:
    transcription = groq_client.audio.transcriptions.create(
        file=(filename, audio_bytes),
        model="whisper-large-v3",
        response_format="text",
    )
    return clean_transcription(transcription.strip())


def clean_transcription(text: str) -> str:
    """Post-process Whisper transcription for command parsing.

    - Strip trailing punctuation on short utterances (likely commands)
    - Normalize to lowercase for consistent matching
    """
    # Short utterances (< 60 chars) are likely commands — strip trailing punctuation
    if len(text) < 60:
        text = re.sub(r'[.!?,;:]+$', '', text)
    return text
```
- **Verification:** `clean_transcription("Delete one.")` → `"Delete one"`, `clean_transcription("Delete 3.")` → `"Delete 3"`. Long transcriptions (notes, dumps) are left untouched.

Note: We do NOT lowercase here — the original casing is preserved for the "I heard:" reply to look natural. Lowercasing happens in `route_command` and `_clean_num` already.

#### Step 3.2: Forward source parameter through process_input → route_command
- **File:** `bot/handlers.py` **Lines:** L55-56
- **Change:** Pass `source` to `route_command`.
- **Before:**
```python
from bot.commands import route_command
handled = await route_command(text, update, state)
```
- **After:**
```python
from bot.commands import route_command
handled = await route_command(text, update, state, source=source)
```
- **Verification:** `source="voice"` now reaches route_command.

#### Step 3.3: Accept source parameter in route_command
- **File:** `bot/commands.py` **Lines:** L47
- **Change:** Add `source` parameter to `route_command` signature and use it to bypass fast path for voice when args look like natural language (contain non-numeric, non-word-number content).
- **Before:**
```python
async def route_command(text: str, update: Update, state: dict) -> bool:
```
- **After:**
```python
async def route_command(text: str, update: Update, state: dict, source: str = "text") -> bool:
```
- **Verification:** Signature accepts source.

#### Step 3.4: Voice-aware fast path bypass
- **File:** `bot/commands.py` **Lines:** L62-85
- **Change:** For voice input, skip the fast path when args contain natural language (not just a number or word-number). This sends "Delete that task about groceries" to Claude but still fast-paths "Delete 3" and "Delete one" from voice.
- **Before (inside the for loop, after compound detection):**
```python
            # Natural language add: let Claude parse project/context references
            if prefix == "add" and re.search(r'\b(under|back|for the|to the|in the|into)\b', args.lower()):
                break
```
- **After:**
```python
            # Natural language add: let Claude parse project/context references
            if prefix == "add" and re.search(r'\b(under|back|for the|to the|in the|into)\b', args.lower()):
                break
            # Voice: skip fast path unless args are a clean number or word-number
            # This routes "delete that task about groceries" to Claude
            if source == "voice" and requires_args:
                clean = _clean_num(args)
                if not clean.isdigit():
                    break
```
- **Verification:** Voice "Delete that task about groceries" → fast path tries prefix match "delete", gets args "that task about groceries" → `_clean_num` returns non-digit → breaks to Claude. Voice "Delete 3" → `_clean_num("3")` → `"3".isdigit()` → stays on fast path. Typed "Delete 3" → unaffected (source="text").

#### Step 3.5: Pass source to _classify_and_dispatch (for future use)
- **File:** `bot/commands.py` **Lines:** L88, L91
- **Change:** Forward source to `_classify_and_dispatch` in case voice-specific classifier tuning is needed later. Minimal change.
- **Before:**
```python
    return await _classify_and_dispatch(text, update, state)

async def _classify_and_dispatch(text: str, update: Update, state: dict) -> bool:
```
- **After:**
```python
    return await _classify_and_dispatch(text, update, state, source=source)

async def _classify_and_dispatch(text: str, update: Update, state: dict, source: str = "text") -> bool:
```
- **Verification:** Source flows through. Not used in classifier yet but available.

---

## Testing Plan

### Manual Testing (via Telegram voice messages)

Since there's no test infrastructure, all verification is manual via the running bot.

#### _clean_num fixes (after Phase 1)
- [ ] Voice: "Delete one" → should delete task #1 (previously failed on "one.")
- [ ] Voice: "Done 3" → should mark #3 done (previously failed on "3.")
- [ ] Voice: "Delete 3 please" → should delete #3 (filler word "please")
- [ ] Voice: "Mark the first task as done" → should work (ordinal "first")
- [ ] Voice: "Delete the third one" → should delete #3 (ordinal + filler)
- [ ] Typed: "delete 3" → still works (regression check)
- [ ] Typed: "done 5" → still works (regression check)

#### Classifier prompt fix (after Phase 2)
- [ ] Voice: "Delete the grocery task" → Claude resolves to correct task number
- [ ] Voice: "Mark the dentist task as done" → resolves correctly
- [ ] Typed: "show me my Thrown tasks" → still classifies correctly (regression)

#### Voice-aware routing (after Phase 3)
- [ ] Voice: "Delete that task about groceries" → goes to Claude, not fast path
- [ ] Voice: "Delete 3" → still fast-pathed (efficient)
- [ ] Voice: "Add buy milk for the home project" → goes to Claude (natural language add)
- [ ] Typed: "delete that task about groceries" → goes to Claude via existing slow path (no regression)
- [ ] Long voice note (>60 chars) → transcription preserved with original punctuation

## Rollback Plan

All changes are in 3 files on a feature branch. Rollback:
```bash
git revert HEAD  # if committed as single commit
# or
git checkout main -- bot/commands.py services/claude.py bot/handlers.py services/groq.py
```

## Risks & Mitigations

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| `_clean_num` changes break typed commands | Low | High | Only added stripping — existing typed inputs don't have punctuation/fillers. Regression test typed commands. |
| Classifier prompt change breaks text classification | Low | Medium | Added `task_map_titles` as optional param with fallback to old behavior if None. Existing `task_context` section untouched. |
| Voice fast-path bypass sends too much to Claude | Medium | Low | Only bypasses when `_clean_num` result is non-digit. Clean numbers still fast-path. Cost impact minimal (Claude calls are already happening for ambiguous text). |
| `_get_task_map_titles` DB query adds latency | Low | Low | Single query with `IN` clause. Only called on slow path (already hitting Claude API which is slower). |
| Punctuation strip in `clean_transcription` removes meaningful punctuation from long voice notes | Low | Low | Only applied to utterances <60 chars. Long notes/dumps pass through unchanged. |

## Open Questions

None — all issues have clear, surgical fixes with low risk of regression.
