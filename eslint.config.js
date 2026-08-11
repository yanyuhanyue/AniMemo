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
    files: ["src/lib/apiCore.js", "src/lib/authSession.js"],
    rules: {
      "no-restricted-globals": [
        "error",
        "window",
        "document",
        "localStorage",
        "sessionStorage",
        "navigator",
        "location",
      ],
      "no-restricted-imports": ["error", {
        paths: [
          "axios",
          "react",
          "react-dom",
          "react-router-dom",
          "./api.js",
          "./webApiTransport.js",
          "./webAuthAdapter.js",
        ],
      }],
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
