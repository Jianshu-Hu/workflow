from __future__ import annotations

import re

from utils.common import WorkflowPaths, clip_text


DISCUSSION_TRANSCRIPT_PROMPT_CHARS = 60000
DISCUSSION_REQUIRED_SECTIONS = [
    "# Discussion",
    "## Task Summary",
    "## Problem Statement",
    "## Constraints",
    "## Current Understanding",
    "## Promising Directions",
    "## Rejected Ideas",
    "## Open Questions",
    "## Next Actions",
]


def strip_terminal_control_sequences(text: str) -> str:
    ansi_pattern = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1B\\))")
    cleaned = ansi_pattern.sub("", text)
    cleaned = cleaned.replace("\r\n", "\n").replace("\r", "\n")
    cleaned = cleaned.replace("\x07", "").replace("\xa0", " ")
    lines = []
    previous_blank = False
    for raw_line in cleaned.splitlines():
        line = raw_line.strip("\x00")
        if line.startswith("Script started on ") or line.startswith("Script done on "):
            continue
        is_blank = not line.strip()
        if is_blank and previous_blank:
            continue
        lines.append(line.rstrip())
        previous_blank = is_blank
    return "\n".join(lines).strip() + ("\n" if lines else "")


def strip_script_log_markers(text: str) -> str:
    text = re.sub(r"^Script started on .*(?:\n|$)", "", text, count=1, flags=re.MULTILINE)
    text = re.sub(r"(?:\r?\n)?Script done on .*$", "", text, count=1, flags=re.MULTILINE)
    return text


def skip_osc_sequence(text: str, start: int) -> int:
    index = start + 2
    while index < len(text):
        if text[index] == "\x07":
            return index + 1
        if text[index] == "\x1b" and index + 1 < len(text) and text[index + 1] == "\\":
            return index + 2
        index += 1
    return len(text)


def skip_dcs_sequence(text: str, start: int) -> int:
    index = start + 2
    while index < len(text):
        if text[index] == "\x1b" and index + 1 < len(text) and text[index + 1] == "\\":
            return index + 2
        index += 1
    return len(text)


def parse_csi_sequence(text: str, start: int) -> tuple[str, int]:
    index = start + 2
    while index < len(text):
        ch = text[index]
        if "@" <= ch <= "~":
            return text[start + 2 : index + 1], index + 1
        index += 1
    return "", len(text)


def normalize_discussion_text_line(line: str) -> str:
    return re.sub(r"\s+", " ", line.replace("\xa0", " ")).strip()


def clean_discussion_input_log(text: str) -> str:
    text = strip_script_log_markers(text)
    submitted_lines: list[str] = []
    buffer: list[str] = []
    cursor = 0

    def commit_line() -> None:
        nonlocal buffer, cursor
        line = normalize_discussion_text_line("".join(buffer))
        if line:
            submitted_lines.append(line)
        buffer = []
        cursor = 0

    index = 0
    while index < len(text):
        ch = text[index]
        if ch == "\x1b":
            if index + 1 >= len(text):
                break
            marker = text[index + 1]
            if marker == "[":
                sequence, next_index = parse_csi_sequence(text, index)
                index = next_index
                if not sequence:
                    continue
                final = sequence[-1]
                params = sequence[:-1]
                amount = 1
                if params and params.split(";")[0].isdigit():
                    amount = int(params.split(";")[0])
                if final == "C":
                    cursor = min(len(buffer), cursor + amount)
                elif final == "D":
                    cursor = max(0, cursor - amount)
                continue
            if marker == "]":
                index = skip_osc_sequence(text, index)
                continue
            if marker == "P":
                index = skip_dcs_sequence(text, index)
                continue
            index += 2
            continue
        if ch in {"\x08", "\x7f"}:
            if cursor > 0:
                cursor -= 1
                del buffer[cursor]
            index += 1
            continue
        if ch in {"\r", "\n"}:
            commit_line()
            index += 1
            continue
        if ord(ch) < 32:
            index += 1
            continue
        if ch == "\t":
            ch = " "
        if cursor == len(buffer):
            buffer.append(ch)
        else:
            buffer.insert(cursor, ch)
        cursor += 1
        index += 1

    commit_line()
    return "\n".join(submitted_lines).strip() + ("\n" if submitted_lines else "")


def extract_user_turns_from_input_log(text: str) -> list[str]:
    cleaned = clean_discussion_input_log(text)
    if not cleaned:
        return []
    return [line.strip() for line in cleaned.splitlines() if line.strip()]


def normalize_assistant_message_lines(lines: list[str]) -> str:
    if not lines:
        return ""
    normalized: list[str] = []
    previous_blank = False
    for raw_line in lines:
        line = re.sub(r"\s+", " ", raw_line.strip())
        if not line:
            if normalized and not previous_blank:
                normalized.append("")
            previous_blank = True
            continue
        starts_new_block = bool(re.match(r"^(-|\*|•|\d+\.)\s", line))
        if normalized and normalized[-1] and not previous_blank and not starts_new_block:
            normalized[-1] = f"{normalized[-1]} {line}"
        else:
            normalized.append(line)
        previous_blank = False
    return "\n".join(part for part in normalized if part is not None).strip()


def clean_discussion_output_log(text: str) -> str:
    text = strip_script_log_markers(text)
    text = re.sub(r"\x1b\[(\d*)C", lambda match: " " * int(match.group(1) or "1"), text)
    return strip_terminal_control_sequences(text)


def is_discussion_approval_line(line: str) -> bool:
    stripped = normalize_discussion_text_line(line)
    compact = re.sub(r"\s+", "", stripped).lower()
    if not compact:
        return False
    exact_matches = {
        "thiscommandrequiresapproval",
        "doyouwanttoproceed?",
        "2.yes,anddontaskagainfor:",
        "3.no",
        "esctocanceltabtoamendctrl+etoexplain",
        "thefilewriterequiresyourapproval.pleasegrantpermissiontowriteto`discussion.md`andi'llproceed.",
    }
    if compact in exact_matches:
        return True
    if compact.startswith("doyouwanttoproceed"):
        return True
    if compact.startswith("thefilewriterequiresyourapproval"):
        return True
    if compact.startswith("pleasegrantpermissiontowriteto"):
        return True
    return False


def is_discussion_tool_noise_line(line: str) -> bool:
    stripped = normalize_discussion_text_line(line)
    compact = re.sub(r"\s+", "", stripped).lower()
    if not compact:
        return False
    if re.fullmatch(r"\* [A-Za-z0-9]{1,3}", stripped):
        return True
    if stripped.startswith("$ ") or stripped.startswith("Bash("):
        return True
    if stripped.startswith(("usage: ", "error: ", "huggingface-cli: error:")):
        return True
    if compact.startswith(("searchingfor", "reading", "listing", "find(")):
        return True
    if "ctrl+otoexpand" in compact:
        return True
    if "requiresapproval" in compact or "grantpermissionto" in compact:
        return True
    shell_fragment_markers = (
        ">/dev/null",
        "2>/dev",
        "&&",
        "| head",
        "xargs ",
        "grep -l",
        "python3 -c",
        "find /home/",
        "ls /home/",
    )
    if any(marker in stripped for marker in shell_fragment_markers):
        return True
    return False


def is_discussion_status_line(line: str) -> bool:
    stripped = line.strip()
    compact = re.sub(r"\s+", "", stripped).lower()
    if not compact:
        return False
    if is_discussion_approval_line(stripped) or is_discussion_tool_noise_line(stripped):
        return True
    if stripped[0] in {"✢", "✶", "✻", "✽", "·", "*"} and ("…" in stripped or "..." in stripped or "tokens" in stripped):
        return True
    if stripped.startswith("⎿ "):
        return True
    if stripped.startswith("Tip: "):
        return True
    if stripped in {"Press Ctrl-C again to exit", "PressCtrl-C again to exit", "Resume this session with:"}:
        return True
    if compact.startswith("claude--resume"):
        return True
    if compact.startswith("0;") or compact.startswith("9;"):
        return True
    return False


def is_fragmented_discussion_noise(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    if is_discussion_approval_line(stripped) or is_discussion_tool_noise_line(stripped):
        return True
    if stripped in {"~", "=", ","}:
        return True
    if stripped.startswith("+ "):
        return True
    if stripped in {"Checking for updates", "Tip: ctrl+s to stash"}:
        return True
    if len(stripped) == 1 and stripped.isalnum():
        return True
    if len(stripped.split()) == 1 and len(stripped) < 12 and stripped[-1] not in ".!?:":
        return True
    tokens = stripped.split()
    short_tokens = sum(1 for token in tokens if len(token) <= 2)
    if len(tokens) >= 4 and short_tokens * 5 >= len(tokens) * 4:
        return True
    if re.fullmatch(r"[\d\s().]+", stripped):
        return True
    return False


def compact_discussion_line(line: str) -> str:
    return re.sub(r"\s+", "", line).lower()


def prefer_discussion_line(existing: str, candidate: str) -> str:
    existing_score = (existing.count(" "), sum(ch.isalpha() for ch in existing), len(existing))
    candidate_score = (candidate.count(" "), sum(ch.isalpha() for ch in candidate), len(candidate))
    return candidate if candidate_score > existing_score else existing


def dedupe_assistant_message_lines(lines: list[str]) -> list[str]:
    deduped: list[str] = []
    for raw_line in lines:
        line = normalize_discussion_text_line(raw_line)
        if not line:
            if deduped and deduped[-1] != "":
                deduped.append("")
            continue
        compact = compact_discussion_line(line)
        replaced = False
        for index in range(max(0, len(deduped) - 3), len(deduped)):
            existing = deduped[index]
            if not existing:
                continue
            existing_compact = compact_discussion_line(existing)
            if compact == existing_compact:
                deduped[index] = prefer_discussion_line(existing, line)
                replaced = True
                break
            if compact and existing_compact and compact in existing_compact:
                deduped[index] = prefer_discussion_line(existing, line)
                replaced = True
                break
            if compact and existing_compact and existing_compact in compact:
                deduped[index] = prefer_discussion_line(existing, line)
                replaced = True
                break
        if not replaced:
            deduped.append(line)
    return deduped


def is_plausible_assistant_content_line(line: str) -> bool:
    stripped = normalize_discussion_text_line(line)
    if not stripped:
        return False
    if (
        is_discussion_status_line(stripped)
        or is_fragmented_discussion_noise(stripped)
        or is_discussion_approval_line(stripped)
        or is_discussion_tool_noise_line(stripped)
    ):
        return False
    if re.match(r"^(-|\*|•|\d+\.)\s", stripped):
        return True
    if stripped.endswith((".", "?", "!", ":")):
        return True
    words = re.findall(r"[A-Za-z0-9_/~.-]+", stripped)
    long_words = sum(1 for word in words if len(word) >= 3)
    return len(words) >= 4 and long_words >= max(2, len(words) // 2)


def is_substantive_assistant_turn(message: str) -> bool:
    text = message.strip()
    if not text:
        return False
    if "\n- " in text or "\n1. " in text:
        return True
    words = re.findall(r"[A-Za-z]{3,}", text)
    return len(words) >= 12


def discussion_word_set(text: str) -> set[str]:
    return {word.lower() for word in re.findall(r"[A-Za-z]{3,}", text)}


def reflected_user_overlap_ratio(text: str, user_turn: str) -> float:
    words = discussion_word_set(text)
    if not words:
        return 0.0
    overlap = words & discussion_word_set(user_turn)
    return len(overlap) / len(words)


def sanitize_assistant_turn_against_user_turns(message: str, user_turns: list[str]) -> str:
    lines = [line for line in message.splitlines()]
    sanitized_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            if sanitized_lines and sanitized_lines[-1] != "":
                sanitized_lines.append("")
            continue
        if is_discussion_approval_line(stripped) or is_discussion_tool_noise_line(stripped):
            continue
        if re.match(r"^(-|\*|•|\d+\.)\s", stripped) or stripped.endswith("?"):
            sanitized_lines.append(stripped)
            continue
        overlap = max((reflected_user_overlap_ratio(stripped, user_turn) for user_turn in user_turns), default=0.0)
        if overlap >= 0.85:
            continue
        sanitized_lines.append(stripped)
    sanitized_message = normalize_assistant_message_lines(sanitized_lines)
    if not sanitized_message:
        return ""
    if "?" not in sanitized_message and "\n- " not in sanitized_message:
        overlap = max((reflected_user_overlap_ratio(sanitized_message, user_turn) for user_turn in user_turns), default=0.0)
        if overlap >= 0.85:
            return ""
    return sanitized_message


def extract_assistant_turns_from_output_log(text: str) -> list[str]:
    cleaned = clean_discussion_output_log(text)
    lines: list[str] = []
    noise_substrings = (
        "claudecode",
        "tipsforgettingstarted",
        "welcomeback",
        "recentactivity",
        "norecentactivity",
        "?forshortcuts",
        "esctointerrupt",
        "apiusagebilling",
        "roosting",
        "schlepping",
        "/effort",
    )
    for raw_line in cleaned.splitlines():
        line = raw_line.rstrip()
        compact = re.sub(r"\s+", "", line).lower()
        if not compact:
            lines.append("")
            continue
        if compact.startswith("scriptstartedon") or compact.startswith("scriptdoneon"):
            continue
        if any(token in compact for token in noise_substrings):
            continue
        if all(ch in "-─╭╮╰╯│└┘┌┐┆┊━═ " for ch in line):
            continue
        if compact in {"❯", "●", "✢", "*", "✶", "✻", "✽", "·"}:
            continue
        if compact.startswith("0;"):
            continue
        lines.append(line)

    first_assistant_index = next((i for i, line in enumerate(lines) if line.lstrip().startswith("● ")), None)
    if first_assistant_index is None:
        return []

    assistant_turns: list[str] = []
    current_lines: list[str] = []
    for raw_line in lines[first_assistant_index:]:
        stripped = raw_line.strip()
        if not stripped:
            if current_lines and current_lines[-1] != "":
                current_lines.append("")
            continue
        if stripped.startswith("● "):
            if current_lines:
                message = normalize_assistant_message_lines(dedupe_assistant_message_lines(current_lines))
                if message and is_substantive_assistant_turn(message):
                    assistant_turns.append(message)
            current_lines = [stripped[2:].strip()]
            continue
        if stripped.startswith("❯"):
            continue
        if is_discussion_status_line(stripped):
            if current_lines:
                message = normalize_assistant_message_lines(dedupe_assistant_message_lines(current_lines))
                if message and is_substantive_assistant_turn(message):
                    assistant_turns.append(message)
                current_lines = []
            continue
        if not is_plausible_assistant_content_line(stripped):
            continue
        current_lines.append(stripped)
    if current_lines:
        message = normalize_assistant_message_lines(dedupe_assistant_message_lines(current_lines))
        if message and is_substantive_assistant_turn(message):
            assistant_turns.append(message)
    return assistant_turns


def build_discussion_transcript(paths: WorkflowPaths) -> str:
    user_turns: list[str] = []
    if paths.discussion_input_log.exists():
        user_turns = extract_user_turns_from_input_log(paths.discussion_input_log.read_text(encoding="utf-8"))
    assistant_turns: list[str] = []
    if paths.discussion_output_log.exists():
        assistant_turns = extract_assistant_turns_from_output_log(paths.discussion_output_log.read_text(encoding="utf-8"))
    if assistant_turns:
        assistant_turns = [
            sanitized
            for sanitized in (
                sanitize_assistant_turn_against_user_turns(turn, user_turns) for turn in assistant_turns
            )
            if sanitized and is_substantive_assistant_turn(sanitized)
        ]

    sections = ["# Discussion Transcript", ""]
    assistant_index = 0
    user_index = 0
    turn_number = 1

    if assistant_turns:
        sections.extend(
            [
                "## Assistant Opening",
                "",
                "Assistant:",
                "",
                assistant_turns[0],
                "",
            ]
        )
        assistant_index = 1

    while user_index < len(user_turns) or assistant_index < len(assistant_turns):
        if user_index < len(user_turns):
            sections.extend(
                [
                    f"## User Turn {turn_number}",
                    "",
                    "User:",
                    "",
                    user_turns[user_index],
                    "",
                ]
            )
            user_index += 1
        if assistant_index < len(assistant_turns):
            sections.extend(
                [
                    f"## Assistant Reply {turn_number}",
                    "",
                    "Assistant:",
                    "",
                    assistant_turns[assistant_index],
                    "",
                ]
            )
            assistant_index += 1
        turn_number += 1

    return "\n".join(sections).rstrip() + "\n"


def build_discussion_summary_prompt(paths: WorkflowPaths, model_hint: str) -> str:
    task_text = paths.task_md.read_text(encoding="utf-8")
    existing_discussion = paths.discussion_md.read_text(encoding="utf-8")
    transcript_raw = paths.discussion_transcript.read_text(encoding="utf-8")
    transcript_text = clip_text(
        strip_terminal_control_sequences(transcript_raw),
        DISCUSSION_TRANSCRIPT_PROMPT_CHARS,
        from_end=True,
    )

    return f"""You are summarizing a kickoff discussion for a coding workflow.

Rewrite `{paths.discussion_md.name}` as the durable structured summary for later workflow stages.
Use the transcript as the ground truth. Preserve concrete user decisions, constraints, links, and open questions.
If the transcript contains assistant claims about edits or actions that were not actually performed, ignore those claims and summarize only the substantive discussion content.

Requirements:
- Return the full contents of `{paths.discussion_md.name}` only. No surrounding explanation.
- Organize the file with these sections in order:
  1. `# Discussion`
  2. `## Task Summary`
  3. `## Problem Statement`
  4. `## Constraints`
  5. `## Current Understanding`
  6. `## Promising Directions`
  7. `## Rejected Ideas`
  8. `## Open Questions`
  9. `## Next Actions`
- Keep the summary concise but specific.
- Prefer flat bullet lists under the section headings when listing multiple items.
- Include related links or references only if they were discussed or are already present in the task file.
- Do not invent facts that are not supported by the transcript or task file.
- If the transcript is ambiguous, capture that ambiguity as an open question instead of guessing.

Model hint: use {model_hint} level reasoning for synthesis, but keep the output practical and compact.

Task file:
```markdown
{task_text.strip()}
```

Existing discussion file:
```markdown
{existing_discussion.strip()}
```

Discussion transcript:
```text
{transcript_text.strip()}
```
"""


def is_valid_discussion_summary(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return False
    compact = re.sub(r"\s+", "", stripped).lower()
    if "requiresapproval" in compact or "grantpermissionto" in compact:
        return False

    search_start = 0
    for header in DISCUSSION_REQUIRED_SECTIONS:
        index = stripped.find(header, search_start)
        if index < 0:
            return False
        search_start = index + len(header)
    return True
