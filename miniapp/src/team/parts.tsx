// The small pieces a staff member is drawn with, shared by the team page, the hiring form and, later,
// the staff member's own view: the coloured avatar and the executor's badge.

import { HARNESS_BADGES, HARNESS_NAMES, Harness, colourVar, initials } from "./team";

export function StaffAvatar({ name, color, size }: { name: string; color: string; size?: "small" }) {
  return (
    <span className={`staff-avatar ${size ?? ""}`} style={{ ["--c" as string]: colourVar(color) }} aria-hidden>
      {initials(name)}
    </span>
  );
}

export function HarnessBadge({ harness, title }: { harness: Harness; title?: string }) {
  return (
    <span className={`harness-badge ${harness}`} title={title ?? HARNESS_NAMES[harness]}>
      {HARNESS_BADGES[harness]}
    </span>
  );
}
