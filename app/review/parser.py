import re
from dataclasses import dataclass
from typing import Optional


@dataclass
class DiffLine:
    """
    A single line from a diff patch, fully parsed.
    
    This is the core data unit. Every line you see in a PR diff
    becomes one of these objects.
    """
    line_type: str            # 'header' | 'added' | 'removed' | 'context'
    content: str              # actual text, with the +/-/space prefix stripped
    new_line_number: Optional[int]  # line number in the NEW file (None for removed lines)
    old_line_number: Optional[int]  # line number in the OLD file (None for added lines)
    position: int             # position in the diff — what GitHub's Review API needs


@dataclass
class ParsedFile:
    """
    All parsed diff data for one file in the PR.
    
    The two key methods here are what the rest of the system uses:
    - get_position_for_line: converts LLM's line number → GitHub's position
    - get_reviewable_line_numbers: tells the LLM which lines it can comment on
    """
    filename: str
    status: str
    lines: list[DiffLine]

    def get_position_for_line(self, new_line_number: int) -> Optional[int]:
        """
        Given a line number in the new file, return the diff position.
        
        This is used to validate and convert LLM output before posting
        to GitHub. If the LLM returns a line number not in our diff,
        this returns None and we fall back to a file-level comment.
        """
        for line in self.lines:
            if (
                line.new_line_number == new_line_number
                and line.line_type in ("added", "context")
            ):
                return line.position
        return None

    def get_reviewable_line_numbers(self) -> list[int]:
        """
        Return all new-file line numbers visible in the diff.
        
        We only tell the LLM to comment on lines it can actually see.
        These are added lines + context lines (not removed lines, which
        no longer exist in the new file).
        """
        return [
            line.new_line_number
            for line in self.lines
            if line.line_type in ("added", "context")
            and line.new_line_number is not None
        ]

    def get_added_line_numbers(self) -> list[int]:
        """Return only the newly added line numbers — the actual changes."""
        return [
            line.new_line_number
            for line in self.lines
            if line.line_type == "added"
            and line.new_line_number is not None
        ]


def parse_patch(patch: str, filename: str, status: str) -> ParsedFile:
    """
    Parse a raw unified diff patch string into structured DiffLine objects.
    
    Walk through the patch line by line:
    - Track position (increments for EVERY line including @@ headers)
    - Track new_line_number (only increments for + and context lines)
    - Track old_line_number (only increments for - and context lines)
    
    The hunk header @@ -old_start,count +new_start,count @@ tells us
    where to start counting line numbers for each chunk.
    """
    if not patch:
        return ParsedFile(filename=filename, status=status, lines=[])

    lines: list[DiffLine] = []
    position = 0
    new_line_num = 0
    old_line_num = 0

    for raw_line in patch.split("\n"):
        if not raw_line:
            continue

        if raw_line.startswith("@@"):
            # Hunk header — counts as a position but has no file line number
            position += 1

            # Parse starting line numbers from the @@ header
            # Format: @@ -old_start,old_count +new_start,new_count @@
            match = re.search(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", raw_line)
            if match:
                # Subtract 1 because we increment BEFORE using the value below
                old_line_num = int(match.group(1)) - 1
                new_line_num = int(match.group(2)) - 1

            lines.append(DiffLine(
                line_type="header",
                content=raw_line,
                new_line_number=None,
                old_line_number=None,
                position=position,
            ))

        elif raw_line.startswith("+"):
            position += 1
            new_line_num += 1
            # Strip the leading + to get the actual code content
            lines.append(DiffLine(
                line_type="added",
                content=raw_line[1:],
                new_line_number=new_line_num,
                old_line_number=None,
                position=position,
            ))

        elif raw_line.startswith("-"):
            position += 1
            old_line_num += 1
            # Strip the leading - to get the actual code content
            lines.append(DiffLine(
                line_type="removed",
                content=raw_line[1:],
                new_line_number=None,
                old_line_number=old_line_num,
                position=position,
            ))

        else:
            # Context line — starts with a space (or rarely no prefix in some diffs)
            position += 1
            new_line_num += 1
            old_line_num += 1
            # Strip the leading space if present
            content = raw_line[1:] if raw_line.startswith(" ") else raw_line
            lines.append(DiffLine(
                line_type="context",
                content=content,
                new_line_number=new_line_num,
                old_line_number=old_line_num,
                position=position,
            ))

    return ParsedFile(filename=filename, status=status, lines=lines)


def parse_pr_files(files: list[dict]) -> list[ParsedFile]:
    """
    Parse all files from get_pr_context() output into ParsedFile objects.
    
    This is the entry point your pipeline will call.
    Input: the 'files' list from get_pr_context()
    Output: list of ParsedFile objects ready for context building
    """
    parsed = []
    for f in files:
        if not f.get("patch"):
            continue
        parsed_file = parse_patch(f["patch"], f["filename"], f["status"])
        parsed.append(parsed_file)
    return parsed


def build_file_context(parsed_file: ParsedFile, full_content: str) -> str:
    """
    Build the context string that will be sent to the LLM.
    
    Two sections:
    1. The diff — shows exactly what changed with line numbers and positions
    2. The full file — gives the LLM surrounding context beyond the diff hunks
    
    Line numbers are included explicitly so the LLM can reference them
    when reporting issues. The LLM returns line numbers; your validator
    converts them to positions before posting to GitHub.
    """
    context_parts = [
        f"File: {parsed_file.filename}",
        f"Status: {parsed_file.status}",
        f"Reviewable lines: {parsed_file.get_reviewable_line_numbers()}",
        "",
        "=== DIFF (changed lines) ===",
    ]

    for line in parsed_file.lines:
        if line.line_type == "header":
            context_parts.append(f"\n{line.content}")
        elif line.line_type == "added":
            context_parts.append(f"+ line {line.new_line_number:>4}: {line.content}")
        elif line.line_type == "removed":
            context_parts.append(f"- (removed): {line.content}")
        elif line.line_type == "context":
            context_parts.append(f"  line {line.new_line_number:>4}: {line.content}")

    if full_content:
        MAX_CONTENT_CHARS = 6000
        truncated = False
        if len(full_content) > MAX_CONTENT_CHARS:
            full_content = full_content[:MAX_CONTENT_CHARS]
            truncated = True

        context_parts.extend([
            "",
            "=== FULL FILE CONTENT ===",
            full_content,
        ])
        if truncated:
            context_parts.append(f"[...file truncated at {MAX_CONTENT_CHARS} characters]")

    return "\n".join(context_parts)