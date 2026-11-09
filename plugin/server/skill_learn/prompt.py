SKILL_REVIEW_INSTRUCTION = """Review the conversation above and propose updates to the skill library.

Skill names and descriptions are already in the system prompt. Do not assume a skill body. If you need bodies, call the existing skill tool once in this response for every skill you need. Do not load them one at a time. Do not call any other tool.

Reply with one JSON object, or with exactly "Nothing to save."

Act on reusable corrections and verified techniques, but only within the
boundary and ownership rules below. If nothing qualifies, reply exactly
"Nothing to save."

Skill boundaries:
- Each skill owns one coherent class of tasks, with clear triggers and
  exclusions. Sharing a session, repository, or technology is not enough
  to combine responsibilities.
- Separate independent lessons before choosing where to save them.
  Code style, Kafka delivery/recovery, and documentation authoring belong
  to separate skills, even when one task involves all three.
- Determine an existing skill's boundary from its description and body,
  not its name or the fact that it was loaded.
- Preserve that boundary. Do not widen its responsibilities or triggering
  task classes to accommodate a new lesson. This applies to SKILL.md and
  every reference, template, and script.
- References may deepen an existing responsibility, never add a new one.
  A pointer to another skill is allowed; copying that skill's procedures
  into this skill is not.

Placement order:
1. Find the existing skill whose established boundary owns the lesson.
   If one exists, reuse it rather than create an overlapping skill.
2. Patch its existing procedure or reference when the lesson corrects or
   improves that material. For a distinct subtopic within the same boundary,
   add a topical reference and a one-line pointer in SKILL.md.
3. Create a new skill only when no existing skill owns the task class.
   Give it a narrow, reusable purpose, clear triggers, and explicit
   exclusions for neighboring responsibilities. Do not create a skill
   for the incidental combination of tasks encountered in one session.
4. If the owning skill is protected or read-only, omit the change.
   Do not create a duplicate or put the lesson into a different skill
   merely because that skill is editable.

Before proposing changes, check: does this teach an existing responsibility
better, or add a new responsibility? Only the first belongs in an existing
skill.

Write procedures and pitfalls, not incident logs. Do not use ticket numbers, PR numbers, dates, or session-specific error text as the skill name or body. Do not save missing binaries, unconfigured credentials, unresolved attempts, or claims that a tool is permanently broken. If a retry worked, save the retry pattern.

A skill marked user-owned, pinned, bundled, hub, or external must not appear in changes. A skill without metadata.origin generated is user-owned. Do not propose a bare delete.

The Skill index below lists the managed global library and, when available, this project's .opencode/skills library. Only generated, unprotected skills in these indexes may be edited. Other host-visible skills are reference-only. Existing names resolve to the project first; use scope "global" or "project" to disambiguate. New skills default to the global library; use scope "project" for project-specific procedures when a project library is listed. Never edit another project's skills or create a duplicate just because an external skill is read-only.

JSON shape:
{"changes":[{"name":"skill-name","action":"create","description":"When to use it.","content":"Procedure body.","files":[{"path":"references/example.md","content":"..."}]}]}
action is create, edit, patch, write_file, or remove_file.
edit replaces SKILL.md. Put the procedure in content and the one-sentence description in description.
patch requires old_string and new_string, and file_path when the file is not SKILL.md.
write_file and remove_file use file_path under references/, templates/, or scripts/. write_file also uses content.
"""


def review_instruction(library, include_index=False, project_library=None):
    if not include_index:
        return SKILL_REVIEW_INSTRUCTION
    index = library.skill_index()
    catalog = index if index else "(no skills)"
    text = f"{SKILL_REVIEW_INSTRUCTION}\nSkill index (global):\n{catalog}"
    if project_library:
        text += f"\nSkill index (project):\n{project_library.skill_index() or '(no skills)'}"
    return text
