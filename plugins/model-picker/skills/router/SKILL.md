---
name: router
description: >
  List and switch model-picker's router profiles: named sets of router settings kept in the user's
  config (MODEL_PICKER_ROUTER_PROFILES), such as a hosted decisions API, a local model behind the
  `command` router, or the keyword rules alone. Use for /model-picker:router, or when the user asks
  which router model-picker uses, or to switch, change, or turn off its router.
argument-hint: '[profile]'
---

# model-picker router

Paths are relative to the directory that contains this SKILL.md.

1. List the profiles:

   ```bash
   python3 ../../hooks/model_picker.py router
   ```

   The current profile is marked `*`. `keywords` is built in: no router, keyword rules only.

2. If the user named a profile (as the argument or in their words), switch to it. Otherwise show the
   list and ask which one, with the profiles as options and the current one marked; don't switch
   without an answer.

   ```bash
   python3 ../../hooks/model_picker.py router <profile>
   ```

3. Show the output as-is. It names the old and new profile and the result of the profile's
   `on_leave` / `on_select` commands (for example starting or stopping a local model server). If one
   says FAILED, say so plainly: the settings were switched anyway, so the router may be unreachable
   until the user fixes that command or switches back. Add nothing else.

If the list says there are no profiles of the user's own yet, show the example from the README's
"Router profiles" section and offer to add their current settings as a first profile. Edit the
config file only after they agree.
