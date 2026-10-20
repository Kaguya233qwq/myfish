# Fishy Frontend

Vue 3 + TypeScript + Vite. Run commands from this directory.

```powershell
npm ci
npm run dev
npm run build
npm run test:e2e
```

`build` runs strict Vue/TypeScript checking and writes `dist/`, which the Fishy backend serves and the Python package includes. Rebuild after changing source files. `dev` starts Vite; its `/api` proxy expects the local Fishy backend configured in `vite.config.ts`.

## Source structure

- `src/App.vue`: application bootstrap, authentication transition, and workspace provider.
- `src/views/`: product, order, job, team, audit, and security pages.
- `src/components/layout/WorkspaceLayout.vue`: navigation, toolbar, notifications, and page switching.
- `src/components/dialogs/`: dialog host and separate forms for each management operation.
- `src/components/`: authentication, dashboard, category picker, and published listing editor.
- `src/composables/useWorkspace.ts`: shared session, fetched data, polling, and operation feedback. One instance is provided by `App.vue`; descendants access its typed context with `useWorkspace()`.
- `src/composables/useProductEditor.ts`: product draft state, validation-related transformations, uploads, and saving.
- `src/composables/useDialogs.ts`: active dialog and selected entities; `useDialogFocus.ts` handles keyboard focus.
- `src/api/client.ts`: JSON requests, session cookies, CSRF headers, and unauthorized responses.
- `src/types.ts` and `src/utils/format.ts`: API models and shared presentation helpers.
- `src/style.css`: shared yellow/white theme, responsive layout, and transitions, including reduced-motion support.

Add page UI to `views/`, independent operations to dialog components, and reusable stateful behavior to composables. Keep root bootstrap small. Current navigation is local page state; it does not define URL routes.

## Browser tests

`npm run test:e2e` uses Playwright with Microsoft Edge and the repository's `.venv` Python interpreter. The test server creates a temporary database and mocks all Fish API calls; it does not use real account data. Workspace interaction tests additionally intercept `/api` requests in the browser.

Tests cover initialization/login, drafts, fixed delivery content, listing edits, listing state, polishing, delivery, jobs, users, mobile navigation, form errors, CSRF, search, session revocation, and dialog keyboard focus. Screenshots are written to ignored `test-results/`. Production assets must be built before browser tests.
