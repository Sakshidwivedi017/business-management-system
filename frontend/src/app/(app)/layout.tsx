import type { ReactNode } from "react";

import { RequireAuth } from "@/components/auth/RequireAuth";
import { ChatProvider } from "@/components/chat/ChatProvider";
import { AppShell } from "@/components/layout/AppShell";

// Application pages require a signed-in user (checked in the browser; the backend checks every request).
// The conversation lives inside RequireAuth, so it never outlives the session that started it.
export default function AppLayout({ children }: { children: ReactNode }) {
  return (
    <RequireAuth>
      <ChatProvider>
        <AppShell>{children}</AppShell>
      </ChatProvider>
    </RequireAuth>
  );
}
