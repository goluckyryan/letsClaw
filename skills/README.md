Skills — written-down know-how the agent picks up when a task matches.

A skill is a directory here holding a `SKILL.md` whose YAML frontmatter has a
`description`:

    skills/letsclaw-dev/SKILL.md
    skills/<name>/SKILL.md
    skills/<name>/scripts/…        anything else the skill wants; the core never reads it

    ---
    name: <name>                   optional; the directory name is what counts
    description: One or two sentences — what the skill is for and when to use it.
    ---

    The body: steps, commands, pitfalls. Plain Markdown.

With `skills.enabled: true` in `config.yaml`, the core puts a **catalog** in
every session's system prompt — each skill's name, description and absolute
path — and the model opens the one it wants with `read_file`. A skill's body
never enters the prompt, so a skill costs its description every turn and its
full text only on the turns that read it. `/behavior` shows the catalog a
window was given; `/skills` lists what is on disk, `/skills <name>` prints one.

The rules the loader holds a skill to, each of which skips the skill with a
warning naming the file rather than failing:

- the directory name matches `^[a-z0-9][a-z0-9._-]*$`, and the `SKILL.md`
  resolves inside this directory (a symlink out of it is not followed)
- the frontmatter parses as YAML and has a non-empty `description`
- the whole file is at most `skills.max_skill_chars` (8 000 by default) — the
  same figure `read_file` clips at, so a listed skill can be read in one go.
  A bigger one should be split
- it sits directly here, not a level deeper

The description is what the model chooses by, so it says *when* to use the
skill, not only what it is. An edited or added skill reaches a live session at
its next rollover, `/clear` or `/new`, the same as a behavior file.

`skills.dirs` adds roots beyond this one — another checkout's skill tree, read
in place. An earlier root wins a name clash. A borrowed root is someone else's
instructions inside your agent's prompt, so it is named explicitly, never by
wildcard.

This README is not a skill: only subdirectories are scanned.
