import { Button } from "@/components/ui/Button";
import { describeMutation } from "@/lib/chat";
import type { MutationStatus } from "@/lib/types";
import styles from "./Chat.module.css";

type MutationCardProps = {
  mutation: MutationStatus;
  /** Confirm/Cancel are offered only while the backend can still accept the answer. */
  confirmable: boolean;
  /** Why a proposal is no longer confirmable here, if it is not. */
  closedReason: "answered" | "expired" | null;
  busy: boolean;
  onConfirm: () => void;
  onCancel: () => void;
};

/** A proposed or finished change, exactly as the backend reported it. */
export function MutationCard({ mutation, confirmable, closedReason, busy, onConfirm, onCancel }: MutationCardProps) {
  const view = describeMutation(mutation);
  const awaiting = mutation.state === "confirmation_required";
  const headingId = `mutation-${mutation.proposal_id ?? "status"}-${mutation.state}`;

  return (
    <section className={styles.mutation} data-tone={view.tone} aria-labelledby={headingId}>
      <header className={styles.mutationHeader}>
        <h3 id={headingId} className={styles.mutationHeading}>
          {view.heading}
        </h3>
        {view.operation && <span className={styles.mutationOperation}>{view.operation}</span>}
      </header>

      {awaiting && (
        <p className={styles.mutationNotice}>
          <strong>Nothing has been changed yet.</strong> The change is made only if you confirm it.
        </p>
      )}

      <p className={styles.mutationMessage}>{mutation.message}</p>

      {view.facts.length > 0 && (
        <dl className={styles.mutationFacts}>
          {view.facts.map((fact) => (
            <div key={fact.label}>
              <dt>{fact.label}</dt>
              <dd>{fact.value}</dd>
            </div>
          ))}
        </dl>
      )}

      {view.lines.length > 0 && (
        <ul className={styles.mutationLines} aria-label="Order lines">
          {view.lines.map((line, index) => (
            <li key={index}>
              <span>{line.item}</span>
              <span>
                {line.quantity} × {line.price} = {line.total}
              </span>
            </li>
          ))}
        </ul>
      )}

      {awaiting && confirmable && (
        <div className={styles.mutationActions}>
          <Button onClick={onConfirm} disabled={busy}>
            Confirm change
          </Button>
          <Button variant="secondary" onClick={onCancel} disabled={busy}>
            Cancel change
          </Button>
          <span className={styles.mutationHint}>Sending a different message also leaves it unconfirmed.</span>
        </div>
      )}
      {awaiting && !confirmable && closedReason === "answered" && (
        <p className={styles.mutationHint}>No longer awaiting confirmation. The outcome appears later in the conversation.</p>
      )}
      {awaiting && !confirmable && closedReason === "expired" && (
        <p className={styles.mutationHint}>
          This proposal is older than 30 minutes and can no longer be confirmed. Ask again if you still want the change.
        </p>
      )}
    </section>
  );
}
