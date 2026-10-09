---
name: writing-for-agents
description: Use when creating or editing skills, AGENTS.md, CLAUDE.md, or instruction documents linked from them.
license: MIT
---

# Writing for agents

1. Identify the task, invocation trigger, and observable result. Read the existing
   instructions and authoritative repository docs before drafting.
2. Choose the location using [the repository skill guide](../../../docs/agents/skills.md).
   Extend an existing skill when it already owns the task; split only when a
   distinct trigger or execution branch earns a separate document.
3. Write the actions in execution order, with a checkable completion criterion
   for each step. Keep definitions and their caveats together. State the desired
   behavior positively; retain explicit prohibitions for hard safety boundaries.
4. Keep the main file focused on what every invocation needs. Move branch-specific
   examples and reference material to sibling files, linking each with the
   condition that requires reading it. Confirm every relative link resolves.
5. Prune sentence by sentence: remove repeated rules, stale advice, generic
   instructions that add no behavior, and copies of facts cheaply found in code
   or configuration. Preserve every required outcome and security constraint.
6. Walk through a representative request and a nearby out-of-scope request.
   Confirm that the trigger selects the right task, the steps reach the intended
   result, and the agent can tell when it is done. Report what was checked and
   any remaining ambiguity.

## Skill packaging

- Use `<skill-id>/SKILL.md` with a lowercase kebab-case ID matching `name`.
- Give `description` concrete invocation conditions; it is the always-loaded
  pointer, while the body is loaded on demand.
- Keep scripts, templates, and references beside the skill and use relative links.
- For OpenCode skills intended only for explicit invocation, set
  `disable-model-invocation: true`. Otherwise keep the description model-visible.

Adapted from Matt Pocock's `writing-for-agents`; see
[provenance](../../../docs/agents/skills.md#upstream-provenance) and [LICENSE](LICENSE).
