import { Button } from "@/components/ui/Button";
import type { AssistantMessage, ChatMessage } from "@/lib/chat";
import { DataBoundary } from "./data/DataBoundary";
import { ReplyData } from "./data/ReplyData";
import { MutationCard } from "./MutationCard";
import styles from "./Chat.module.css";

type MessageItemProps = {
  message: ChatMessage;
  busy: boolean;
  retryable: boolean;
  confirmable: boolean;
  closedReason: "answered" | "expired" | null;
  onRetry: (id: string) => void;
  onAnswer: (id: string, decision: "confirm" | "cancel") => void;
};

export function MessageItem({ message, busy, retryable, confirmable, closedReason, onRetry, onAnswer }: MessageItemProps) {
  if (message.role === "user") {
    return (
      <li className={`${styles.message} ${styles.user}`}>
        <span className="sr-only">You:</span>
        <div className={styles.bubble}>{message.content}</div>
        {message.status === "failed" && (
          <div className={styles.failure} role="alert">
            <span>{message.error ?? "This message could not be sent."}</span>
            {retryable && (
              <Button variant="secondary" size="sm" onClick={() => onRetry(message.id)} disabled={busy}>
                Retry
              </Button>
            )}
          </div>
        )}
      </li>
    );
  }

  return (
    <li className={`${styles.message} ${styles.assistant}`}>
      <span className={styles.author}>Assistant</span>
      <AssistantBody
        message={message}
        busy={busy}
        confirmable={confirmable}
        closedReason={closedReason}
        onAnswer={onAnswer}
      />
    </li>
  );
}

function AssistantBody({
  message,
  busy,
  confirmable,
  closedReason,
  onAnswer,
}: Pick<MessageItemProps, "busy" | "confirmable" | "closedReason" | "onAnswer"> & { message: AssistantMessage }) {
  const { mutation } = message;
  // A change step's reply text is the backend's own status message: show it once, in the card.
  const showText = !mutation || message.content.trim() !== mutation.message.trim();
  return (
    <>
      {showText && <div className={styles.bubble}>{message.content}</div>}
      <DataBoundary>
        <ReplyData message={message} />
      </DataBoundary>
      {mutation && (
        <MutationCard
          mutation={mutation}
          confirmable={confirmable}
          closedReason={closedReason}
          busy={busy}
          onConfirm={() => onAnswer(message.id, "confirm")}
          onCancel={() => onAnswer(message.id, "cancel")}
        />
      )}
    </>
  );
}
