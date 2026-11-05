import type { components } from "@/api/schema";

export type Health = components["schemas"]["Health"];

export type Notice = {
  key: string;
  /** Short and plain: for anyone who can see the Overview. */
  title: string;
  detail: string;
  /** The full text, for administrators (it can hold paths and driver messages). */
  technical: string;
};

const ACCESS_DENIED = /access is denied|operating system error 5\b|permission denied/i;

/** GET /api/health's problems as short notices. Every problem it lists gets one, so nothing is silently dropped. */
export function healthNotices(health: Health | undefined): Notice[] {
  if (!health?.problems?.length) return [];
  const notices: Notice[] = [];
  const known = new Set<string>();
  const { backup, disk } = health;
  if (backup.problem) {
    known.add(backup.problem);
    const failed = backup.ok === false;
    const technical = backup.error ?? backup.problem;
    notices.push({
      key: "backup",
      title: failed ? "Nightly backup failed" : "Nightly backup overdue",
      detail: !failed
        ? "It hasn't run on schedule"
        : ACCESS_DENIED.test(technical)
          ? "Backup folder access denied"
          : "See the details for the reason",
      technical,
    });
  }
  if (disk.problem) {
    known.add(disk.problem);
    notices.push({
      key: "disk",
      title: "Low disk space",
      detail: `${(disk.free_bytes / 1e9).toFixed(1)} GB free on the data disk`,
      technical: disk.problem,
    });
  }
  for (const camera of health.cameras) {
    if (!camera.problem) continue;
    known.add(camera.problem);
    notices.push({
      key: `camera-${camera.name}`,
      title: `Camera ${camera.name} needs a look`,
      detail: camera.state === "running" ? "No picture" : `It is ${camera.state}`,
      technical: camera.problem,
    });
  }
  health.problems
    .filter((problem) => !known.has(problem))
    .forEach((problem, i) =>
      notices.push({ key: `other-${i}`, title: "System warning", detail: "See the details", technical: problem }),
    );
  return notices;
}
