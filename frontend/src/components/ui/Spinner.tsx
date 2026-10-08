import styles from "./Spinner.module.css";

type SpinnerProps = {
  size?: "sm" | "md" | "lg";
  /** Read by screen readers; shown next to the spinner when `showLabel` is set. */
  label?: string;
  showLabel?: boolean;
};

export function Spinner({ size = "md", label = "Loading", showLabel = false }: SpinnerProps) {
  return (
    <span className={styles.wrapper} role="status">
      <span className={`${styles.spinner} ${styles[size]}`} aria-hidden="true" />
      <span className={showLabel ? styles.label : "sr-only"}>{label}</span>
    </span>
  );
}

/** A centred spinner with a message, for a section or page that is loading. */
export function LoadingState({ label = "Loading…" }: { label?: string }) {
  return (
    <div className={styles.block}>
      <Spinner size="lg" label={label} showLabel />
    </div>
  );
}
