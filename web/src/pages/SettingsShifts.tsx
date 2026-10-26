import { useQuery, useQueryClient } from "@tanstack/react-query";
import { History, Pencil, Plus, Trash2, Undo2 } from "lucide-react";
import { Fragment, useState } from "react";

import { api } from "@/api/client";
import type { Shift, ShiftVersion } from "@/api/types";
import { ReasonDialog } from "@/components/ReasonDialog";
import { TableSkeleton } from "@/components/states";
import { Badge, Card, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Field, Input } from "@/components/ui/input";
import { ErrorState, Td, Th } from "@/components/ui/misc";
import { today } from "@/lib/utils";

type Form = {
  name: string;
  start: string;
  end: string;
  grace_min: string;
  in_window_before_min: string;
  out_window_after_min: string;
  effective_from: string;
};

const NEW: Form = {
  name: "",
  start: "08:00",
  end: "16:00",
  grace_min: "30",
  in_window_before_min: "120",
  out_window_after_min: "240",
  effective_from: "",
};

function formOf(s: Shift): Form {
  return {
    name: s.name,
    start: s.start,
    end: s.end,
    grace_min: String(s.grace_min),
    in_window_before_min: String(s.in_window_before_min),
    out_window_after_min: String(s.out_window_after_min),
    effective_from: today(),
  };
}

function times(v: { start: string; end: string; crosses_midnight: boolean }): string {
  return `${v.start}–${v.end}${v.crosses_midnight ? " (next day)" : ""}`;
}

/**
 * Settings > Shifts: the shifts the roster uses. Changing a shift's times (or grace, or windows) applies from a date
 * the user picks, default today: the days before keep the times they had, so attendance history never changes. A
 * change that hasn't started yet can be withdrawn. A shift on the roster can't be deleted.
 */
export function ShiftsTab() {
  const queryClient = useQueryClient();
  const shifts = useQuery({
    queryKey: ["shifts", "settings"],
    queryFn: () => api.get("/api/shifts", { query: { page_size: 500 } }),
  });
  const [editing, setEditing] = useState<Shift | "new" | null>(null);
  const [form, setForm] = useState<Form>(NEW);
  const [deleting, setDeleting] = useState<Shift | null>(null);
  const [withdrawing, setWithdrawing] = useState<[Shift, ShiftVersion] | null>(null);
  const [history, setHistory] = useState<number | null>(null);
  const refresh = () => {
    for (const k of [["shifts"], ["roster"], ["attendance"], ["overview"], ["reports"]]) {
      void queryClient.invalidateQueries({ queryKey: k });
    }
  };
  const open = (s: Shift | "new") => {
    setEditing(s);
    setForm(s === "new" ? NEW : formOf(s));
  };
  const set = (k: keyof Form) => (e: React.ChangeEvent<HTMLInputElement>) => setForm({ ...form, [k]: e.target.value });

  const save = async (reason: string) => {
    const numbers = {
      grace_min: Number(form.grace_min),
      in_window_before_min: Number(form.in_window_before_min),
      out_window_after_min: Number(form.out_window_after_min),
    };
    if (editing === "new") {
      await api.post("/api/shifts", {
        body: { name: form.name.trim(), start: form.start, end: form.end, ...numbers, reason },
      });
    } else if (editing) {
      const changedTimes =
        form.start !== editing.start ||
        form.end !== editing.end ||
        numbers.grace_min !== editing.grace_min ||
        numbers.in_window_before_min !== editing.in_window_before_min ||
        numbers.out_window_after_min !== editing.out_window_after_min;
      await api.patch("/api/shifts/{shift_id}", {
        path: { shift_id: editing.id },
        body: {
          reason,
          ...(form.name.trim() !== editing.name ? { name: form.name.trim() } : {}),
          ...(changedTimes
            ? { start: form.start, end: form.end, ...numbers, effective_from: form.effective_from || today() }
            : {}),
        },
      });
    }
    refresh();
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle>Shifts</CardTitle>
        <Button size="sm" onClick={() => open("new")}>
          <Plus /> Add shift
        </Button>
      </CardHeader>
      <p className="px-4 pb-2 text-xs text-muted-foreground">
        The IN window opens before the start and closes at the shift&apos;s midpoint; the OUT window runs from the
        midpoint to after the end. IN up to start + grace is on time; OUT before end − grace is an early leave. A time
        change applies from the date you pick; earlier days keep their times.
      </p>
      {shifts.error ? (
        <ErrorState error={shifts.error} retry={() => void shifts.refetch()} />
      ) : shifts.isPending ? (
        <TableSkeleton rows={3} />
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-sm" aria-label="Shifts">
            <thead>
              <tr>
                <Th>Name</Th>
                <Th>Times</Th>
                <Th className="text-right">Grace</Th>
                <Th className="text-right">IN window opens</Th>
                <Th className="text-right">OUT window closes</Th>
                <Th>In force since</Th>
                <Th className="text-right">Roster days</Th>
                <Th />
              </tr>
            </thead>
            <tbody>
              {shifts.data.items.map((s) => {
                const planned = s.versions.filter((v) => v.planned);
                return (
                  <Fragment key={s.id}>
                    <tr aria-label={s.name}>
                      <Td className="font-medium">{s.name}</Td>
                      <Td className="tabular-nums">
                        {times(s)}
                        {planned.map((v) => (
                          <Badge key={v.id} tone="warn" className="ml-1" title={`From ${v.effective_from}`}>
                            {times(v)} from {v.effective_from}
                          </Badge>
                        ))}
                      </Td>
                      <Td className="text-right tabular-nums">{s.grace_min} min</Td>
                      <Td className="text-right tabular-nums">{s.in_window_before_min} min before</Td>
                      <Td className="text-right tabular-nums">{s.out_window_after_min} min after</Td>
                      <Td>{s.effective_from ?? "the start"}</Td>
                      <Td className="text-right tabular-nums">{s.roster_days}</Td>
                      <Td className="text-right whitespace-nowrap">
                        <Button
                          variant="ghost"
                          size="sm"
                          aria-label={`History of ${s.name}`}
                          onClick={() => setHistory(history === s.id ? null : s.id)}
                        >
                          <History />
                        </Button>
                        <Button variant="ghost" size="sm" aria-label={`Edit ${s.name}`} onClick={() => open(s)}>
                          <Pencil />
                        </Button>
                        <Button
                          variant="ghost"
                          size="sm"
                          aria-label={`Delete ${s.name}`}
                          disabled={s.roster_days > 0}
                          title={s.roster_days ? "On the roster: change those days first" : undefined}
                          onClick={() => setDeleting(s)}
                        >
                          <Trash2 />
                        </Button>
                      </Td>
                    </tr>
                    {history === s.id && (
                      <tr>
                        <Td colSpan={8} className="bg-muted/40">
                          <ul className="flex flex-col gap-1 text-xs" aria-label={`Versions of ${s.name}`}>
                            {s.versions.map((v) => (
                              <li key={v.id} className="flex flex-wrap items-center gap-2">
                                <span className="w-28 font-medium">
                                  {v.effective_from ? `from ${v.effective_from}` : "from the start"}
                                </span>
                                <span className="tabular-nums">{times(v)}</span>
                                <span className="text-muted-foreground">
                                  grace {v.grace_min}, IN −{v.in_window_before_min}, OUT +{v.out_window_after_min} min ·
                                  by {v.created_by}, {v.created.slice(0, 16).replace("T", " ")}
                                </span>
                                {v.planned && (
                                  <Button
                                    variant="ghost"
                                    size="sm"
                                    onClick={() => setWithdrawing([s, v])}
                                    aria-label={`Withdraw the change from ${v.effective_from}`}
                                  >
                                    <Undo2 /> Withdraw
                                  </Button>
                                )}
                              </li>
                            ))}
                          </ul>
                        </Td>
                      </tr>
                    )}
                  </Fragment>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      <ReasonDialog
        open={editing !== null}
        onOpenChange={(o) => !o && setEditing(null)}
        title={editing === "new" ? "Add a shift" : `Edit ${editing?.name ?? ""}`}
        description={
          editing === "new"
            ? "An end at or before the start means the shift ends the next day (a night shift, dated the day it starts)."
            : "New times, grace or windows apply from the date below; the days before keep the times they had. A new name applies everywhere."
        }
        confirmLabel="Save"
        onConfirm={save}
      >
        <div className="grid grid-cols-2 gap-3">
          <Field label="Name" htmlFor="shift-name" className="col-span-2">
            <Input id="shift-name" value={form.name} onChange={set("name")} />
          </Field>
          <Field label="Start" htmlFor="shift-start">
            <Input id="shift-start" type="time" value={form.start} onChange={set("start")} />
          </Field>
          <Field label="End" htmlFor="shift-end">
            <Input id="shift-end" type="time" value={form.end} onChange={set("end")} />
          </Field>
          <Field label="Grace (min)" htmlFor="shift-grace">
            <Input
              id="shift-grace"
              type="number"
              min={0}
              max={720}
              value={form.grace_min}
              onChange={set("grace_min")}
            />
          </Field>
          <Field label="IN window opens (min before start)" htmlFor="shift-before">
            <Input
              id="shift-before"
              type="number"
              min={0}
              max={720}
              value={form.in_window_before_min}
              onChange={set("in_window_before_min")}
            />
          </Field>
          <Field label="OUT window closes (min after end)" htmlFor="shift-after">
            <Input
              id="shift-after"
              type="number"
              min={0}
              max={720}
              value={form.out_window_after_min}
              onChange={set("out_window_after_min")}
            />
          </Field>
          {editing !== "new" && (
            <Field label="Times apply from" htmlFor="shift-from">
              <Input id="shift-from" type="date" value={form.effective_from} onChange={set("effective_from")} />
            </Field>
          )}
        </div>
      </ReasonDialog>
      <ReasonDialog
        open={deleting !== null}
        onOpenChange={(o) => !o && setDeleting(null)}
        title={`Delete ${deleting?.name ?? ""}?`}
        confirmLabel="Delete"
        destructive
        onConfirm={async (reason) => {
          if (deleting)
            await api.delete("/api/shifts/{shift_id}", { path: { shift_id: deleting.id }, query: { reason } });
          refresh();
        }}
      />
      <ReasonDialog
        open={withdrawing !== null}
        onOpenChange={(o) => !o && setWithdrawing(null)}
        title="Withdraw this planned change?"
        description={
          withdrawing
            ? `${withdrawing[0].name} keeps ${times(withdrawing[0])} after ${withdrawing[1].effective_from}.`
            : undefined
        }
        confirmLabel="Withdraw"
        onConfirm={async (reason) => {
          if (withdrawing)
            await api.delete("/api/shifts/{shift_id}/versions/{version_id}", {
              path: { shift_id: withdrawing[0].id, version_id: withdrawing[1].id },
              query: { reason },
            });
          refresh();
        }}
      />
    </Card>
  );
}
