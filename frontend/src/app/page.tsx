import { redirect } from "next/navigation";

// The application starts at the dashboard. Layer 11 adds the signed-in check in front of it.
export default function Home() {
  redirect("/dashboard");
}
