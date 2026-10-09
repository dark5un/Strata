// eslint flat config — scoped to the harness tab (ADR-0005: no build step, vanilla ES2020).
// app.js/index.html belong to upstream; only our own JS is gated here.
export default [
  {
    files: ["serve/web/harness.js"],
    languageOptions: {
      ecmaVersion: 2020,
      sourceType: "script",
      globals: {
        // provided by app.js (the tab is lazy-loaded by loadHarness())
        $: "readonly", icon: "readonly", esc: "readonly", fmt: "readonly",
        headers: "readonly", store: "readonly", toast: "readonly",
        lastMetrics: "readonly",
        // browser
        document: "readonly", window: "readonly", fetch: "readonly",
        AbortController: "readonly", TextDecoder: "readonly",
        encodeURIComponent: "readonly", JSON: "readonly", Math: "readonly",
        Set: "readonly", navigator: "readonly",
        setTimeout: "readonly", URL: "readonly", innerHeight: "readonly",
      },
    },
    rules: {
      "no-undef": "error",
      "no-unused-vars": ["warn", {args: "none"}],
      "no-dupe-keys": "error",
      "no-dupe-args": "error",
      "no-unreachable": "error",
      "no-cond-assign": "error",
      "no-constant-condition": "warn",
      "no-empty": ["warn", {allowEmptyCatch: true}],
    },
  },
];
