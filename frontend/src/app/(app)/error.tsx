"use client"; // Error boundaries must be Client Components

import { ErrorState } from "@/components/ui/ErrorState";

// The error itself is not shown: it may hold internal details.
export default function AppError({ retry }: { error: Error & { digest?: string }; retry: () => void }) {
  return <ErrorState message="This page could not be displayed. Please try again." onRetry={retry} />;
}
