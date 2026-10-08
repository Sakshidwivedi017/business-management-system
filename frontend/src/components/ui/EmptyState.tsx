import type { ReactNode } from "react";

import styles from "./States.module.css";

type EmptyStateProps = {
  title: string;
  /** What the user can do next. */
  description?: ReactNode;
  action?: ReactNode;
  icon?: ReactNode;
};

export function EmptyState({ title, description, action, icon }: EmptyStateProps) {
  return (
    <div className={styles.state}>
      {icon && (
        <div className={styles.icon} aria-hidden="true">
          {icon}
        </div>
      )}
      <h3 className={styles.title}>{title}</h3>
      {description && <p className={styles.description}>{description}</p>}
      {action && <div className={styles.action}>{action}</div>}
    </div>
  );
}
