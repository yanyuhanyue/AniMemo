import js from "@eslint/js";
import globals from "globals";
import reactHooks from "eslint-plugin-react-hooks";

const sourceFiles = [
  "src/**/*.{js,jsx}",
  "plugins/**/frontend/**/*.{js,jsx}",
  "bridges/**/pages/**/*.js",
];

const nodeFiles = [
  "tests/**/*.mjs",
  "scripts/**/*.mjs",
  "vite.config.mjs",
  "postcss.config.js",
  "tailwind.config.js",
];

export default [
  {
    ignores: [
      "dist/**",
      "node_modules/**",
      "public/plugin-runtime/**",
    ],
  },
  {
    files: [...sourceFiles, ...nodeFiles],
    ...js.configs.recommended,
    languageOptions: {
      ecmaVersion: "latest",
      sourceType: "module",
      parserOptions: {
        ecmaFeatures: { jsx: true },
      },
    },
    rules: {
      ...js.configs.recommended.rules,
      "no-unused-vars": "off",
      "no-useless-assignment": "off",
      "preserve-caught-error": "off",
    },
  },
  {
    files: sourceFiles,
    languageOptions: {
      globals: globals.browser,
    },
    plugins: {
      "react-hooks": reactHooks,
    },
    rules: {
      "react-hooks/rules-of-hooks": "error",
    },
  },
  {
    files: nodeFiles,
    languageOptions: {
      globals: {
        ...globals.browser,
        ...globals.node,
      },
    },
  },
];
