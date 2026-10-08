import { Button } from "./Button";
import styles from "./States.module.css";

type ErrorStateProps = {
  title?: string;
  /** A user-facing message, such as ApiError.message. Never raw exception text. */
  message?: string;
  onRetry?: () => void;
  retryLabel?: string;
};

export function ErrorState({
  title = "Something went wrong",
  message = "Please try again. If the problem continues, contact your administrator.",
  onRetry,
  retryLabel = "Try again",
}: ErrorStateProps) {
  return (
    <div className={`${styles.state} ${styles.error}`} role="alert">
      <div className={styles.icon} aria-hidden="true">
        <svg width="20" height="20" viewBox="0 0 20 20" fill="none">
          <circle cx="10" cy="10" r="8" stroke="currentColor" strokeWidth="1.5" />
          <path d="M10 6v5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
          <circle cx="10" cy="13.75" r="0.9" fill="currentColor" />
        </svg>
      </div>
      <h3 className={styles.title}>{title}</h3>
      <p className={styles.description}>{message}</p>
      {onRetry && (
        <div className={styles.action}>
          <Button variant="secondary" size="sm" onClick={onRetry}>
            {retryLabel}
          </Button>
        </div>
      )}
    </div>
  );
}
