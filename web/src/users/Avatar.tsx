import type { User } from "../protocol";

/** Up to two initials of a name, uppercased («Ana López» -> «AL»). */
export function initials(name: string): string {
  const words = name.trim().split(/\s+/).filter(Boolean);
  const letters = words.length === 1 ? [...words[0]].slice(0, 2) : words.slice(0, 2).map((w) => [...w][0]);
  return letters.join("").toLocaleUpperCase("es");
}

/** A stable hue (0-5) per user id, so each initials circle keeps its colour. */
function tone(id: string): number {
  let sum = 0;
  for (const ch of id) sum = (sum + ch.charCodeAt(0)) % 6;
  return sum;
}

/** The user's photo, or their initials on a coloured circle when there is none. Decorative: the
 * control that holds it carries the accessible name. */
export default function Avatar({ user, size = "md" }: { user: User; size?: "sm" | "md" }) {
  const className = `user-avatar user-avatar-${size}`;
  if (user.photo_url) return <img className={className} src={user.photo_url} alt="" />;
  return (
    <span className={`${className} user-avatar-tone-${tone(user.id)}`} aria-hidden="true">
      {initials(user.name)}
    </span>
  );
}
