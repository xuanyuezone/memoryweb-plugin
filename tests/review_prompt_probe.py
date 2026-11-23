#!/usr/bin/env python3
"""End-to-end probe: background-review prompt override (feat/review-prompt-override).

Run inside the official image with the plugin installed and HERMES_HOME set to
a profile whose config selects the memory face:

    python3 review_prompt_probe.py on    # memory.provider=memoryweb profile
    python3 review_prompt_probe.py off   # profile without the memory face

Proves, against the real runtime:
  1. register(ctx) reports the swap when the memory face is active
  2. the live selection path — spawn_background_review_thread — returns the
     plugin text for all three scopes (memory-only, skills-only, combined)
  3. getattr precedence on a bare AIAgent instance hits the class attribute
  4. a per-instance setattr still shadows the override (per-agent route)
  5. with the memory face off, the official prompts are untouched
"""
import sys

FAILURES = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILURES.append(name)


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "on"
    # ON: import the installed copy under $HERMES_HOME/plugins/<name> (the
    # shape PluginManager loads). OFF: import the source tree, but with
    # HERMES_HOME pointing at a profile whose config lacks the memory face —
    # proving the module can be loaded while the guard refuses the swap.
    import os

    if mode == "on":
        sys.path.insert(0, os.path.join(os.environ.get("HERMES_HOME", ""), "plugins"))
    else:
        # source copy at a path whose leaf dir is literally named "memoryweb"
        # (import by package name requires it)
        sys.path.insert(0, os.environ.get("OFF_SRC", "/tmp/off-src"))
    import memoryweb as mw

    register = mw.register
    _REVIEW_PROMPT_OVERRIDES = mw._REVIEW_PROMPT_OVERRIDES

    diag = register(None)
    installed = diag.get("review_prompts_installed") or []
    active = diag.get("memory_face_active")

    if mode == "on":
        check("memory face detected as active", active is True, f"diag={diag}")
        check("all three prompts installed", sorted(installed) == sorted(_REVIEW_PROMPT_OVERRIDES),
              f"installed={installed}")

        import agent.background_review as br
        from run_agent import AIAgent

        # 2. the live selection path (returns (target, prompt); target not run).
        # Resolve the attribute name from the runtime's OWN scope map —
        # v0.23.0 and v0.23.3 spell the skills name differently.
        agent = object.__new__(AIAgent)  # bare instance: no __init__, class attrs visible
        for scope in ((True, False), (False, True), (True, True)):
            _target, prompt = br.spawn_background_review_thread(
                agent, [], review_memory=scope[0], review_skills=scope[1])
            expected = _REVIEW_PROMPT_OVERRIDES.get(br._PROMPT_NAME_BY_SCOPE[scope])
            check(f"spawn scope {scope} picks the plugin prompt", prompt == expected)

        # 3. class-attribute hit through instance getattr.
        for name, text in _REVIEW_PROMPT_OVERRIDES.items():
            check(f"instance getattr hits override ({name})",
                  getattr(agent, name) == text)

        # 4. per-instance shadowing still works (the documented per-agent route).
        agent._MEMORY_REVIEW_PROMPT = "per-agent custom"
        _target, prompt = br.spawn_background_review_thread(
            agent, [], review_memory=True, review_skills=False)
        check("per-instance setattr shadows the class override", prompt == "per-agent custom")

        # 5. the swapped texts must not teach the file stores.
        check("memory prompt does not direct file writes",
              "do not write USER.md or MEMORY.md" in _REVIEW_PROMPT_OVERRIDES["_MEMORY_REVIEW_PROMPT"])
        check("combined prompt does not direct file writes",
              "do not write USER.md or MEMORY.md" in _REVIEW_PROMPT_OVERRIDES["_COMBINED_REVIEW_PROMPT"])
    else:
        check("memory face detected as inactive", active is False, f"diag={diag}")
        check("no prompts installed", installed == [], f"installed={installed}")
        import agent.background_review as br
        from run_agent import AIAgent
        agent = object.__new__(AIAgent)
        # Selection-path truth: spawn must return the official module texts.
        # (Note: the AIAgent class attribute for skills is _SKILL_REVIEW_PROMPT
        # singular while the scope map looks up the plural name — the official
        # skills prompt always resolves via the module fallback, so the spawn
        # path is the only honest comparison for it.)
        for scope, module_const in (
            ((True, False), br._MEMORY_REVIEW_PROMPT),
            ((False, True), br._SKILL_REVIEW_PROMPT),
            ((True, True), br._COMBINED_REVIEW_PROMPT),
        ):
            _target, prompt = br.spawn_background_review_thread(
                agent, [], review_memory=scope[0], review_skills=scope[1])
            check(f"official prompt intact for scope {scope}", prompt == module_const)

    print(f"REVIEW-PROMPT PROBE [{mode}] " + ("GREEN" if not FAILURES else f"RED ({len(FAILURES)} failed)"))
    return 0 if not FAILURES else 1


if __name__ == "__main__":
    sys.exit(main())
