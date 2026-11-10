// ESLint flat config for the Admin v2 browser scripts.
// These files are classic scripts (no modules), loaded in order by admin_v2.html.
import js from "@eslint/js";
import globals from "globals";

export default [
  {
    ignores: ["node_modules/**", "dist/**"],
  },
  {
    files: ["resolver/static/admin-v2/**/*.js"],
    ...js.configs.recommended,
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: "script",
      globals: {
        ...globals.browser,
      },
    },
  },
];
