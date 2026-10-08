"use client";

import { createContext, useContext, useState, useSyncExternalStore, type ReactNode } from "react";

import { useAuth } from "@/components/auth/AuthProvider";
import { createChatStore, type ChatState, type ChatStore } from "@/lib/chat";

const ChatContext = createContext<ChatStore | null>(null);

/**
 * Holds the tab's conversation while the user moves between pages. It sits inside
 * RequireAuth, which remounts its children per user, so signing out (or a 401 ending the
 * session) discards the conversation with it.
 */
export function ChatProvider({ children }: { children: ReactNode }) {
  const { authFetch } = useAuth();
  const [store] = useState(() => createChatStore({ fetcher: authFetch }));
  return <ChatContext.Provider value={store}>{children}</ChatContext.Provider>;
}

export function useChat(): { state: ChatState; store: ChatStore } {
  const store = useContext(ChatContext);
  if (!store) throw new Error("useChat must be used inside ChatProvider");
  const state = useSyncExternalStore(store.subscribe, store.getState, store.getState);
  return { state, store };
}
