import { resourceUrl } from "@/api/client";
import { cn } from "@/lib/utils";

function initials(name: string): string {
  const letters = name
    .trim()
    .split(/\s+/)
    .map((word) => word[0] ?? "")
    .join("");
  return (letters.slice(0, 2) || "?").toUpperCase();
}

/** A person's picture if there is one, otherwise their initials. Decorative: the name is always written next to it. */
export function Avatar({ name, src, className }: { name: string; src?: string | null; className?: string }) {
  const box = cn("size-9 shrink-0 rounded-full", className);
  if (src) return <img src={resourceUrl(src)} alt="" loading="lazy" className={cn(box, "object-cover")} />;
  return (
    <span
      aria-hidden
      className={cn(box, "flex items-center justify-center bg-accent text-xs font-semibold text-accent-foreground")}
    >
      {initials(name)}
    </span>
  );
}
