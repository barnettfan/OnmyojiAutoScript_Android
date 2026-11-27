---
name: oas-task-authoring
description: Create and extend OAS game automation tasks with the local task-authoring MCP, including emulator screenshots, rule assets, task skeletons, and verified task-flow implementation.
---

# OAS Task Authoring

Use this skill when creating, extending, annotating, or implementing an OAS task in this repository. Use the `oas-task-authoring` MCP at `http://127.0.0.1:22288/mcp`; it is available only after the user manually starts the OAS Web service on port `22288`. Do not start or stop that service.

## Authorization boundary

Reading task code, configuration, rules, screenshots, and emulator status is allowed for analysis. Perform a repository write only when the user explicitly says **创建**、**保存**、**加入规则** or **实现**. Control the emulator only when the user explicitly says **点击**、**滑动** or **长按**, using the coordinates the user approved or has just identified. After every emulator operation, immediately call `capture_screenshot` and inspect the result. Never delete or overwrite existing task code, rules, or assets without asking the user for confirmation first.

Do not operate the annotator through a browser or Playwright. Use the MCP directly so screenshots are returned as images and device commands use the same capture session.

## Create a task

Before calling `create_task_scaffold`, collect and show any missing items explicitly:

- PascalCase task name, Chinese display name, optional English display name;
- one existing `ConfigMenu` category;
- skeleton type: `basic` or `battle`;
- scheduler intervals/priority and other requested business configuration.

The MCP creates a disabled task only. It adds the task to the selected existing menu category, configuration model/template, i18n, and the end of that category's priority segment. It refuses same-name overwrite. `basic` contains `GameUi`; `battle` also includes common battle and soul-switch configuration. Extending an existing task never creates a new directory: analyze the existing task and implement only after the user authorizes it.

## Capture, templates, and rules

1. Create a session and select an existing emulator configuration with `start_capture`.
2. Use `capture_screenshot`, or use `upload_screenshot` for a user-provided local image.
3. For template matching, preserve a stable UI element in the **Front** crop and give **Back** only the area in which it should be searched. For OCR, Front is the text ROI and Back is the contextual search/validation area.
4. Use `save_cropped_template`, `test_rule`, and inspect the returned test result before calling `save_rules`.
5. `save_rules` writes the JSON and regenerates the task `assets.py`; call it only after explicit user authorization. Existing JSON or image replacement requires confirmation.

## Implement a task flow

Before writing flow code, obtain all of these for each state transition. If any item is absent, state exactly what is missing and ask; do not infer it from an ambiguous screenshot:

- entry page and navigation route;
- stable visible state marker and its rule/asset;
- trigger or guard condition;
- operation (click/swipe/long-click or passive wait) and target;
- expected successor state;
- success, failure, timeout, recovery, and terminal exits;
- post-completion page and scheduler behavior.

Implement only the described transitions. Reuse `GameUi`, common battle components, generated asset classes, `TaskEnd`, and scheduling conventions already used by neighboring tasks. Add Chinese comments for every new method, key branch, and configuration field. Verify with rule tests, Python compilation, focused tests, and—when the user has started OAS and authorized an action—an explicit screenshot/action/screenshot check.
