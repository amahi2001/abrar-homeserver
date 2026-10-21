const fs = require("node:fs");
const path = require("node:path");
const restored = new WeakSet();

// Supported Playwright initPage hook; cookies stay inside this project's home.
exports.default = async ({ page }) => {
  const context = page.context();
  if (restored.has(context)) return;
  restored.add(context);
  const state = path.join(process.env.HOME, ".local/share/dev-ui/browser-state.json");
  if (fs.existsSync(state)) await context.setStorageState(state);
};
