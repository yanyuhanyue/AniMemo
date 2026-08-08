/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{js,jsx}"],
  theme: {
    extend: {
      colors: {
        ink: "#111111",
        cream: "#fff5ee",
        coral: "#ff6b6b",
        pink: "#ff8fab",
        teal: "#4ecdc4",
        yellow: "#ffe66d",
        admin: {
          page: "var(--admin-color-page)",
          surface: "var(--admin-color-surface)",
          subtle: "var(--admin-color-surface-subtle)",
          muted: "var(--admin-color-surface-muted)",
          text: "var(--admin-color-text)",
          "text-muted": "var(--admin-color-text-muted)",
          border: "var(--admin-color-border)",
          primary: "var(--admin-color-primary)",
          success: "var(--admin-color-success)",
          warning: "var(--admin-color-warning)",
          danger: "var(--admin-color-danger)",
        },
      },
      boxShadow: {
        brutal: "6px 6px 0 #111111",
        "admin-sm": "var(--admin-shadow-sm)",
        admin: "var(--admin-shadow-panel)",
      },
      transitionDuration: {
        fast: "var(--admin-duration-fast)",
        normal: "var(--admin-duration-normal)",
      },
    },
  },
  plugins: [],
};
