import type { HTMLAttributes, ReactNode } from "react";

import styles from "./Card.module.css";

type CardProps = HTMLAttributes<HTMLElement> & {
  title?: ReactNode;
  description?: ReactNode;
  /** Shown at the right of the header, e.g. a button. */
  actions?: ReactNode;
  /** Remove body padding, for content that runs edge to edge such as tables. */
  flush?: boolean;
};

export function Card({ title, description, actions, flush = false, className, children, ...rest }: CardProps) {
  const hasHeader = title || description || actions;
  return (
    <section {...rest} className={[styles.card, className].filter(Boolean).join(" ")}>
      {hasHeader && (
        <header className={styles.header}>
          <div className={styles.heading}>
            {title && <h2 className={styles.title}>{title}</h2>}
            {description && <p className={styles.description}>{description}</p>}
          </div>
          {actions && <div className={styles.actions}>{actions}</div>}
        </header>
      )}
      <div className={flush ? undefined : styles.body}>{children}</div>
    </section>
  );
}
