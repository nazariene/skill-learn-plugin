SKILL_REVIEW_INSTRUCTION = """Review the conversation above and propose updates to the skill library.

Skill names and descriptions are already in the system prompt. Do not assume a skill body. If you need bodies, call the existing skill tool once in this response for every skill you need. Do not load them one at a time. Do not call any other tool.

Reply with one JSON object, or with exactly "Nothing to save."

Be active: a user correction, a reusable technique, or a skill that was wrong should become an update. A smooth session with no correction and no new technique ends with exactly "Nothing to save."

Preference order:
1. Patch a generated skill loaded in the conversation, or an existing generated class-level skill that covers the work.
2. Add one topical file under references/, templates/, or scripts/, and add a one-line pointer in SKILL.md.
3. Create a new skill only when nothing existing covers the class.

Write procedures and pitfalls, not incident logs. Do not use ticket numbers, PR numbers, dates, or session-specific error text as the skill name or body. Do not save missing binaries, unconfigured credentials, unresolved attempts, or claims that a tool is permanently broken. If a retry worked, save the retry pattern.

A skill marked user-owned, pinned, bundled, hub, or external must not appear in changes. A skill without metadata.origin generated is user-owned. Do not propose a bare delete.

JSON shape:
{"changes":[{"name":"skill-name","action":"create","description":"When to use it.","content":"Procedure body.","files":[{"path":"references/example.md","content":"..."}]}]}
action is create, edit, patch, write_file, or remove_file.
edit replaces SKILL.md. Put the procedure in content and the one-sentence description in description.
patch requires old_string and new_string, and file_path when the file is not SKILL.md.
write_file and remove_file use file_path under references/, templates/, or scripts/. write_file also uses content.
"""


def review_instruction(library, include_index=False):
    if not include_index:
        return SKILL_REVIEW_INSTRUCTION
    index = library.skill_index()
    catalog = index if index else "(no skills)"
    return f"{SKILL_REVIEW_INSTRUCTION}\nSkill index:\n{catalog}"
