"use client";

import { useRef, useState, type SubmitEvent } from "react";

import { Button } from "@/components/ui/Button";
import { TextField } from "@/components/ui/TextField";
import {
  SESSION_END_MESSAGES,
  loginErrorMessage,
  loginFieldErrors,
  validateLogin,
  type LoginFieldErrors,
} from "@/lib/auth";
import { useAuth } from "./AuthProvider";
import styles from "./LoginForm.module.css";

/** Email and password sign-in. On success AuthProvider holds the session; LoginScreen navigates on. */
export function LoginForm() {
  const { endReason, login } = useAuth();

  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [fieldErrors, setFieldErrors] = useState<LoginFieldErrors>({});
  const [formError, setFormError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [dismissedNotice, setDismissedNotice] = useState(false);
  const emailRef = useRef<HTMLInputElement>(null);
  const passwordRef = useRef<HTMLInputElement>(null);

  const notice = !dismissedNotice && endReason ? SESSION_END_MESSAGES[endReason] : null;

  async function handleSubmit(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    if (submitting) return;
    setDismissedNotice(true);
    setFormError(null);

    const errors = validateLogin({ email, password });
    setFieldErrors(errors);
    if (errors.email || errors.password) {
      (errors.email ? emailRef : passwordRef).current?.focus();
      return;
    }

    setSubmitting(true);
    try {
      await login({ email, password });
    } catch (err) {
      const serverFieldErrors = loginFieldErrors(err);
      setFieldErrors(serverFieldErrors);
      setFormError(loginErrorMessage(err));
      setPassword("");
      (serverFieldErrors.email ? emailRef : passwordRef).current?.focus();
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <form className={styles.form} onSubmit={handleSubmit} noValidate aria-busy={submitting || undefined}>
      {notice && (
        <p className={styles.notice} role="status">
          {notice}
        </p>
      )}
      {formError && (
        <p className={styles.error} role="alert">
          {formError}
        </p>
      )}
      <TextField
        ref={emailRef}
        label="Email"
        type="email"
        name="email"
        autoComplete="username"
        inputMode="email"
        autoFocus
        required
        value={email}
        onChange={(e) => setEmail(e.target.value)}
        error={fieldErrors.email}
        disabled={submitting}
      />
      <TextField
        ref={passwordRef}
        label="Password"
        type="password"
        name="password"
        autoComplete="current-password"
        required
        value={password}
        onChange={(e) => setPassword(e.target.value)}
        error={fieldErrors.password}
        disabled={submitting}
      />
      <Button type="submit" fullWidth loading={submitting}>
        {submitting ? "Signing in…" : "Sign in"}
      </Button>
    </form>
  );
}
