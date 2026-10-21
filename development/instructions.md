# Development workspace

You are inside this project's persistent Linux development container. Complete development here. Source and home persist; tools are installed. No Docker orchestration is needed inside the container.

- Prefix shell commands with `rtk`; use `rtk proxy <command>` for unsupported commands or exact machine-readable output. Use targeted `rg` searches and compact command output.
- Apply Ponytail's smallest complete solution and Caveman's concise communication. Read those skills once when coding; load other skills only when relevant. Preserve clarity, validation, and meaningful checks.
- Inspect repository instructions, current Git branch, and local changes before editing. Preserve existing work. Reuse installed tools and project conventions. Install only missing project dependencies; caches persist in your home.
- Complete authorized work without repeated confirmation. Ask only for missing information or actions outside the task's authority. Report real failures; never claim unrun checks passed.
- Work on the prepared `agent/*` branch. Never force-push, merge, change visibility, or publish packages without explicit instruction. Never commit secrets, environment files, dependencies, or agent state.
- After completing the task, automatically run appropriate checks and commit/push only your task's files using `dev-finish "message" --check "test command" -- file ...`. If no test suite exists, choose a meaningful build/syntax/smoke check. Existing staged changes require review first. Failed pushes leave commits locally; report and retry when connectivity returns.
- Preview ports 3000, 5173, and 8000 have host loopback mappings shown by `dev list` on the host. Servers must listen on `0.0.0.0` inside the container.
- End with result, checks, and any remaining limitation. Do not dump logs.
