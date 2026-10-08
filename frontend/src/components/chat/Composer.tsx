"use client";

import { useId, type KeyboardEvent, type Ref } from "react";

import { Button } from "@/components/ui/Button";
import { MAX_MESSAGE_LENGTH, checkMessage } from "@/lib/chat";
import styles from "./Chat.module.css";

const COUNTER_FROM = MAX_MESSAGE_LENGTH - 500;

type ComposerProps = {
  value: string;
  onChange: (value: string) => void;
  onSubmit: () => void;
  busy: boolean;
  textareaRef: Ref<HTMLTextAreaElement>;
};

/** Message input: Enter sends, Shift+Enter adds a line. */
export function Composer({ value, onChange, onSubmit, busy, textareaRef }: ComposerProps) {
  const id = useId();
  const hintId = `${id}-hint`;
  const check = checkMessage(value);
  const length = value.trim().length;

  function handleKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    // Not while an input method is composing text (e.g. choosing characters).
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      onSubmit();
    }
  }

  return (
    <form
      className={styles.composer}
      onSubmit={(event) => {
        event.preventDefault();
        onSubmit();
      }}
    >
      <label htmlFor={id} className="sr-only">
        Message the assistant
      </label>
      <textarea
        ref={textareaRef}
        id={id}
        className={styles.textarea}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        onKeyDown={handleKeyDown}
        placeholder="Ask about stock, locations, purchase orders…"
        rows={2}
        maxLength={MAX_MESSAGE_LENGTH}
        disabled={busy}
        aria-describedby={hintId}
      />
      <div className={styles.composerFooter}>
        <span id={hintId} className={styles.composerHint}>
          {length >= COUNTER_FROM
            ? `${length.toLocaleString("en-IN")} / ${MAX_MESSAGE_LENGTH.toLocaleString("en-IN")} characters`
            : "Enter to send · Shift+Enter for a new line"}
        </span>
        <Button type="submit" disabled={busy || !check.ok} loading={busy}>
          {busy ? "Sending…" : "Send"}
        </Button>
      </div>
    </form>
  );
}
