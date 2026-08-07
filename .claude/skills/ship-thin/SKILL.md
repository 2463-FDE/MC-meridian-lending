---
name: ship-thin
description: Enforce build-minimal / anti-over-engineering discipline on a task. Use when the user says "ship thin", "keep it simple", "no over-engineering", "smallest version", "MVP this", "don't gold-plate", or asks to scope a feature down before building. Turns a request into the smallest change that delivers the value, names what was deliberately cut, and refuses speculative abstraction.
---

# Ship Thin

Goal: deliver the **smallest change that actually solves the stated problem**. Nothing built for an imagined future.

## Procedure

1. **State the one job.** Write the single concrete outcome the user needs, in one sentence. If you can't, ask — don't guess and build extra.

2. **Cut list.** Before writing code, list what you are deliberately NOT building and why. Examples: config options, extra params, error paths for inputs that can't occur yet, abstraction layers, new files, new deps. The user can pull any item back in, but the default is out.

3. **Reuse before create.**
   - Edit an existing file over creating a new one.
   - Extend an existing function over adding a parallel one.
   - Use stdlib / already-installed packages over a new dependency. Ask before adding any dep.

4. **No abstraction until the 3rd repeat.** One caller = inline it. Two callers = maybe. Three = extract. No base class, factory, interface, or generic wrapper with a single implementation.

5. **Solve the case in front of you.** Not the general case. Hardcode what is currently constant; parameterize only what actually varies today.

6. **Build the slice.** Write it. Then re-read the diff and delete anything that isn't exercised by the one job from step 1 — unused options, dead branches, "just in case" hooks.

## Output

Reply with:
- **Job:** the one sentence.
- **Cut:** bullet list of what was left out (so nothing is silently missing).
- Then the change.

## Refusals

- If asked to "make it flexible/extensible" with no second concrete use case, push back: name the second use case or keep it concrete.
- If scope creeps mid-task, stop and re-confirm the one job.
