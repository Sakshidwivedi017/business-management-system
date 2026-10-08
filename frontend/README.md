# lecxe-chatbot — frontend

Next.js 16 (App Router), React 19 and strict TypeScript, with no UI, chart or test libraries.
The project overview, architecture, setup and demo flow are in the [root README](../README.md).

## Commands

```bash
npm ci                       # install dependencies (first time)
cp .env.example .env.local   # first time: NEXT_PUBLIC_API_BASE_URL, default http://localhost:8000
npm run dev                  # development server on http://localhost:3000
npm run build && npm run start
npm test                     # logic tests (Node's built-in test runner, src/**/*.test.ts)
npm run typecheck
npm run lint
```

The backend must be running for sign-in, the dashboard and chat.

## Layout

| Path | Contents |
| --- | --- |
| `src/app/login`, `src/app/(app)/dashboard`, `src/app/(app)/chat` | routes; `/` redirects to the dashboard |
| `src/components/auth` | session provider, sign-in form, route guard |
| `src/components/dashboard` | role-based dashboard panels |
| `src/components/chat` | conversation, composer, confirmation card, tables and charts (`data/`) |
| `src/components/layout`, `src/components/ui` | app shell and shared primitives |
| `src/lib` | API client, session, chat state, dashboard and visualization logic, with their tests |

Notes:

- The session (token and expiry only) is kept in `sessionStorage`, and the conversation in memory.
- Confirm and Cancel only send "confirm" / "cancel" as chat messages; the backend decides and
  executes.
- Tables and charts are rendered only from the backend's `data` payload.
- This Next.js version differs from older releases; see `AGENTS.md` before changing framework code.
