import { cn } from "@/lib/utils";

/**
 * A drawn entrance or exit: a pale building, glass doors, a camera over them and a plant. Decorative and generic (it
 * doesn't show the real gate). The exit is the entrance mirrored; both follow the theme's colours.
 */
export function GateIllustration({ direction, className }: { direction: "IN" | "OUT"; className?: string }) {
  const mirror = direction === "OUT" ? "translate(240 0) scale(-1 1)" : undefined;
  return (
    <svg viewBox="0 0 240 150" aria-hidden className={cn("h-auto w-full", className)}>
      <g transform={mirror}>
        <ellipse cx="124" cy="140" rx="108" ry="7" fill="var(--primary)" opacity="0.08" />
        {/* the building */}
        <path d="M44 22 L196 12 V136 H44 Z" fill="var(--card)" stroke="var(--border)" />
        <path d="M44 22 L22 34 V136 H44 Z" fill="var(--muted)" stroke="var(--border)" />
        <path d="M44 22 L196 12 V24 L44 34 Z" fill="var(--muted)" />
        {/* the frame and the doors */}
        <rect x="74" y="44" width="92" height="92" rx="1.5" fill="var(--muted-foreground)" opacity="0.55" />
        <rect x="80" y="50" width="39" height="86" fill="var(--accent)" />
        <rect x="121" y="50" width="39" height="86" fill="var(--accent)" />
        <path
          d="M84 50 L104 50 L88 136 L84 136 Z M126 50 L144 50 L130 136 L126 136 Z"
          fill="var(--card)"
          opacity="0.6"
        />
        <rect x="113" y="86" width="2.5" height="20" rx="1" fill="var(--foreground)" opacity="0.55" />
        <rect x="124.5" y="86" width="2.5" height="20" rx="1" fill="var(--foreground)" opacity="0.55" />
        {/* the camera */}
        <path d="M170 34 V28" stroke="var(--muted-foreground)" strokeWidth="2.5" strokeLinecap="round" />
        <rect
          x="154"
          y="33"
          width="26"
          height="11"
          rx="5.5"
          transform="rotate(-14 167 38)"
          fill="var(--card)"
          stroke="var(--muted-foreground)"
        />
        <circle cx="156" cy="42" r="3.2" fill="var(--foreground)" />
        {/* the plant */}
        <path d="M34 112 C26 100 24 90 28 78 C33 88 35 100 36 112 Z" fill="var(--primary)" opacity="0.75" />
        <path d="M38 112 C38 98 42 88 50 80 C51 92 47 104 41 113 Z" fill="var(--primary)" opacity="0.6" />
        <path d="M36 112 C33 102 34 92 38 84 C41 94 41 104 39 113 Z" fill="var(--primary)" />
        <path d="M26 112 H50 L47 136 H29 Z" fill="var(--card)" stroke="var(--border)" />
      </g>
    </svg>
  );
}
