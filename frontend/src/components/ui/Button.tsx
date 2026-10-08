import Link from "next/link";
import type { ButtonHTMLAttributes, ComponentProps } from "react";

import { Spinner } from "./Spinner";
import styles from "./Button.module.css";

export type ButtonVariant = "primary" | "secondary" | "ghost" | "danger";
export type ButtonSize = "sm" | "md";

type ButtonProps = ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: ButtonVariant;
  size?: ButtonSize;
  /** Disables the button and shows a spinner in place of the leading icon. */
  loading?: boolean;
  fullWidth?: boolean;
};

export function Button({
  variant = "primary",
  size = "md",
  loading = false,
  fullWidth = false,
  disabled,
  type = "button",
  className,
  children,
  ...rest
}: ButtonProps) {
  const classes = buttonClasses(variant, size, fullWidth, className);
  return (
    <button {...rest} type={type} className={classes} disabled={disabled || loading} aria-busy={loading || undefined}>
      {loading && <Spinner size="sm" label="Working" />}
      {children}
    </button>
  );
}

type ButtonLinkProps = ComponentProps<typeof Link> & { variant?: ButtonVariant; size?: ButtonSize };

/** A navigation link styled as a button. */
export function ButtonLink({ variant = "secondary", size = "md", className, ...rest }: ButtonLinkProps) {
  return <Link {...rest} className={buttonClasses(variant, size, false, className)} />;
}

function buttonClasses(variant: ButtonVariant, size: ButtonSize, fullWidth: boolean, className?: string): string {
  return [styles.button, styles[variant], styles[size], fullWidth && styles.fullWidth, className]
    .filter(Boolean)
    .join(" ");
}
