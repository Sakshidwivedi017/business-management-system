import type { SVGProps } from "react";

type IconProps = SVGProps<SVGSVGElement>;

function Icon(props: IconProps) {
  return (
    <svg
      width="18"
      height="18"
      viewBox="0 0 20 20"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      {...props}
    />
  );
}

export function DashboardIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <rect x="3" y="3" width="6" height="7" rx="1" />
      <rect x="11" y="3" width="6" height="4" rx="1" />
      <rect x="11" y="9" width="6" height="8" rx="1" />
      <rect x="3" y="12" width="6" height="5" rx="1" />
    </Icon>
  );
}

export function ChatIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M4 4.5h12a1 1 0 0 1 1 1v7a1 1 0 0 1-1 1H9l-3.5 3v-3H4a1 1 0 0 1-1-1v-7a1 1 0 0 1 1-1Z" />
      <path d="M7 8.5h6M7 11h4" />
    </Icon>
  );
}

/** The product mark shown in the sidebar and on the login page. */
export function LogoMark(props: IconProps) {
  return (
    <svg width="24" height="24" viewBox="0 0 24 24" aria-hidden="true" {...props}>
      <rect width="24" height="24" rx="5" fill="var(--color-accent)" />
      <path d="M7 7v10h10" stroke="var(--color-text-inverse)" strokeWidth="2" fill="none" strokeLinecap="round" />
      <path d="M11 13.5 13.5 11l2 2L18 9.5" stroke="var(--color-text-inverse)" strokeWidth="2" fill="none"
        strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}
