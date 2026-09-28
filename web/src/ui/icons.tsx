/**
 * The app's small line icons: inline SVG drawn with `currentColor`, so each takes the colour of
 * the text around it, and hidden from assistive technology (the button or label names the action).
 */

import type { ReactNode } from "react";

interface IconProps {
  size?: number;
}

function Icon({ size = 16, strokeWidth = 2, children }: IconProps & { strokeWidth?: number; children: ReactNode }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={strokeWidth}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      {children}
    </svg>
  );
}

/** An outline bin: deleting or discarding. */
export function TrashIcon({ size }: IconProps) {
  return (
    <Icon size={size}>
      <path d="M3 6h18" />
      <path d="M8 6V4h8v2" />
      <path d="M6 6l1 14h10l1-14" />
      <path d="M10 11v6M14 11v6" />
    </Icon>
  );
}

/** A cross: cancelling, closing. */
export function CloseIcon({ size }: IconProps) {
  return (
    <Icon size={size}>
      <path d="M6 6l12 12M18 6L6 18" />
    </Icon>
  );
}

/** A tick: confirming. */
export function CheckIcon({ size }: IconProps) {
  return (
    <Icon size={size} strokeWidth={2.5}>
      <path d="M5 12.5l4.5 4.5L19 7.5" />
    </Icon>
  );
}

/** A warning triangle: an action that cannot be undone from here. */
export function WarningIcon({ size }: IconProps) {
  return (
    <Icon size={size}>
      <path d="M12 3.5L2.5 20h19L12 3.5z" />
      <path d="M12 10v4.5" />
      <path d="M12 17.5h.01" />
    </Icon>
  );
}

/** A question mark in a circle: a plain question. */
export function QuestionIcon({ size }: IconProps) {
  return (
    <Icon size={size}>
      <circle cx="12" cy="12" r="9" />
      <path d="M9.5 9.5a2.5 2.5 0 1 1 3.5 2.3c-.6.3-1 .8-1 1.5v.7" />
      <path d="M12 17h.01" />
    </Icon>
  );
}

/** A counter-clockwise arrow: bringing an earlier version back. */
export function RestoreIcon({ size }: IconProps) {
  return (
    <Icon size={size}>
      <path d="M4 5v5h5" />
      <path d="M4.5 14.5A8 8 0 1 0 6.3 6.3L4 10" />
    </Icon>
  );
}
