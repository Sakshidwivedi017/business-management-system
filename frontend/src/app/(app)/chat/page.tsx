import type { Metadata } from "next";

import { Chat } from "@/components/chat/Chat";

export const metadata: Metadata = { title: "Assistant" };

export default function ChatPage() {
  return <Chat />;
}
