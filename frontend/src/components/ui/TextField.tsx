import { useId, type InputHTMLAttributes, type Ref } from "react";

import styles from "./TextField.module.css";

type TextFieldProps = Omit<InputHTMLAttributes<HTMLInputElement>, "id"> & {
  label: string;
  error?: string;
  ref?: Ref<HTMLInputElement>;
};

/** A labelled input with its validation message linked for assistive technology. */
export function TextField({ label, error, className, ref, ...rest }: TextFieldProps) {
  const id = useId();
  const errorId = `${id}-error`;
  return (
    <div className={[styles.field, className].filter(Boolean).join(" ")}>
      <label htmlFor={id} className={styles.label}>
        {label}
      </label>
      <input
        {...rest}
        ref={ref}
        id={id}
        className={`${styles.input} ${error ? styles.invalid : ""}`}
        aria-invalid={error ? true : undefined}
        aria-describedby={error ? errorId : undefined}
      />
      {error && (
        <p id={errorId} className={styles.error}>
          {error}
        </p>
      )}
    </div>
  );
}
