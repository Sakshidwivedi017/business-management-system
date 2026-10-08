"use client";

import { useEffect, useRef, useState } from "react";

import { PageHeader } from "@/components/layout/PageHeader";
import { Button } from "@/components/ui/Button";
import { ChatIcon } from "@/components/ui/icons";
import { SUGGESTED_PROMPTS, canRetry, checkMessage, isConfirmable, proposalClosedReason } from "@/lib/chat";
import { useChat } from "./ChatProvider";
import { Composer } from "./Composer";
import { MessageItem } from "./MessageItem";
import styles from "./Chat.module.css";

// How often the page re-checks whether a proposal is still within its 30 minutes.
const CLOCK_INTERVAL_MS = 30_000;

function useNow(intervalMs: number): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(timer);
  }, [intervalMs]);
  return now;
}

export function Chat() {
  const { state, store } = useChat();
  const now = useNow(CLOCK_INTERVAL_MS);
  const [draft, setDraft] = useState("");
  const listRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const busy = state.pendingId !== null;
  const empty = state.messages.length === 0;

  // Keep the latest message (or the "thinking" indicator) in view.
  useEffect(() => {
    const list = listRef.current;
    if (list) list.scrollTop = list.scrollHeight;
  }, [state.messages, busy]);

  // Back to the input once a reply has arrived.
  useEffect(() => {
    if (!busy) textareaRef.current?.focus();
  }, [busy]);

  function submit() {
    if (busy || !checkMessage(draft).ok) return;
    void store.send(draft);
    setDraft("");
  }

  function newConversation() {
    store.reset();
    setDraft("");
    textareaRef.current?.focus();
  }

  return (
    <div className={styles.page}>
      <PageHeader
        title="AI Assistant"
        description="Ask about inventory, procurement, or business operations."
        actions={
          <Button variant="secondary" onClick={newConversation} disabled={empty && !draft}>
            New conversation
          </Button>
        }
      />

      <div className={styles.window}>
        <div ref={listRef} className={styles.scroller}>
          {empty ? (
            <div className={styles.empty}>
              <div className={styles.emptyIcon} aria-hidden="true">
                <ChatIcon width={22} height={22} />
              </div>
              <h2 className={styles.emptyTitle}>How can I help?</h2>
              <p className={styles.emptyText}>
                Answers come from your live inventory and procurement data, within what your role can access. Any
                change you ask for is shown to you for confirmation first.
              </p>
              <ul className={styles.suggestions} aria-label="Suggested questions">
                {SUGGESTED_PROMPTS.map((prompt) => (
                  <li key={prompt}>
                    <button type="button" className={styles.suggestion} onClick={() => void store.send(prompt)}>
                      {prompt}
                    </button>
                  </li>
                ))}
              </ul>
            </div>
          ) : (
            <ol className={styles.messages} role="log" aria-live="polite" aria-label="Conversation">
              {state.messages.map((message) => (
                <MessageItem
                  key={message.id}
                  message={message}
                  busy={busy}
                  retryable={canRetry(state, message.id)}
                  confirmable={isConfirmable(state, message.id, now)}
                  closedReason={proposalClosedReason(state, message.id, now)}
                  onRetry={(id) => void store.retry(id)}
                  onAnswer={(id, decision) => void store.answerProposal(id, decision)}
                />
              ))}
            </ol>
          )}
          {busy && (
            <div className={styles.thinking} role="status">
              <span className={styles.dots} aria-hidden="true">
                <span />
                <span />
                <span />
              </span>
              Assistant is working on it…
            </div>
          )}
        </div>

        <Composer value={draft} onChange={setDraft} onSubmit={submit} busy={busy} textareaRef={textareaRef} />
      </div>
    </div>
  );
}
