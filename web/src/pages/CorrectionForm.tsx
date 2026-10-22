import { zodResolver } from "@hookform/resolvers/zod";
import { useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useForm } from "react-hook-form";
import { toast } from "sonner";
import { z } from "zod";

import { ApiError, api } from "@/api/client";
import type { AttendanceEvent } from "@/api/types";
import { Button } from "@/components/ui/button";
import { Field, Input, NativeSelect, Textarea } from "@/components/ui/input";
import { hhmm } from "@/lib/utils";

export const correctionSchema = z
  .object({
    action: z.enum(["add", "change"]),
    direction: z.enum(["IN", "OUT"]),
    event_id: z.string(),
    time: z.string().regex(/^\d{2}:\d{2}$/, "Pick a time"),
    reason: z.string().trim().min(1, "A reason is required (it goes to the audit log)"),
  })
  .refine((v) => v.action === "add" || v.event_id !== "", { path: ["event_id"], message: "Pick the event to change" });
export type CorrectionValues = z.infer<typeof correctionSchema>;

/** Add an IN/OUT or change an event's time, for one person and day. The original time is kept by the API. */
export function CorrectionForm({
  personId,
  date,
  events,
  onDone,
}: {
  personId: number;
  date: string;
  events: AttendanceEvent[];
  onDone?: () => void;
}) {
  const queryClient = useQueryClient();
  const [error, setError] = useState<string | null>(null);
  const changeable = events.filter((e) => !e.voided);
  const form = useForm<CorrectionValues>({
    resolver: zodResolver(correctionSchema),
    defaultValues: { action: "add", direction: "OUT", event_id: "", time: "", reason: "" },
  });
  const action = form.watch("action");
  const errors = form.formState.errors;

  const onSubmit = form.handleSubmit(async (v) => {
    setError(null);
    const timestamp = `${date}T${v.time}:00`;
    try {
      await api.post("/api/attendance/corrections", {
        body:
          v.action === "add"
            ? { action: "add", person_id: personId, direction: v.direction, timestamp, reason: v.reason }
            : { action: "change", event_id: Number(v.event_id), timestamp, reason: v.reason },
      });
      toast.success(v.action === "add" ? `${v.direction} added at ${v.time}` : `Time changed to ${v.time}`);
      form.reset({ action: "add", direction: "OUT", event_id: "", time: "", reason: "" });
      for (const key of [["attendance"], ["events"], ["overview"], ["person"]]) {
        void queryClient.invalidateQueries({ queryKey: key });
      }
      onDone?.();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    }
  });

  return (
    <form onSubmit={onSubmit} className="flex flex-col gap-2" noValidate aria-label="Add correction">
      <div className="grid grid-cols-2 gap-2">
        <Field label="Correction" htmlFor="c-action">
          <NativeSelect id="c-action" {...form.register("action")}>
            <option value="add">Add an IN or OUT</option>
            <option value="change" disabled={changeable.length === 0}>
              Change a time
            </option>
          </NativeSelect>
        </Field>
        {action === "add" ? (
          <Field label="Direction" htmlFor="c-direction">
            <NativeSelect id="c-direction" {...form.register("direction")}>
              <option value="IN">IN</option>
              <option value="OUT">OUT</option>
            </NativeSelect>
          </Field>
        ) : (
          <Field label="Event" htmlFor="c-event" error={errors.event_id?.message}>
            <NativeSelect id="c-event" aria-invalid={!!errors.event_id} {...form.register("event_id")}>
              <option value="">Choose…</option>
              {changeable.map((e) => (
                <option key={e.id} value={e.id}>
                  {e.direction} {hhmm(e.timestamp)} ({e.camera})
                </option>
              ))}
            </NativeSelect>
          </Field>
        )}
      </div>
      <Field label={action === "add" ? "Time" : "New time"} htmlFor="c-time" error={errors.time?.message}>
        <Input id="c-time" type="time" aria-invalid={!!errors.time} {...form.register("time")} />
      </Field>
      <Field label="Reason (required)" htmlFor="c-reason" error={errors.reason?.message}>
        <Textarea
          id="c-reason"
          aria-invalid={!!errors.reason}
          placeholder="e.g. forgot to look at the exit camera"
          {...form.register("reason")}
        />
      </Field>
      {error && (
        <p role="alert" className="rounded-md bg-destructive/10 px-2 py-1.5 text-sm text-destructive">
          {error}
        </p>
      )}
      <Button type="submit" disabled={form.formState.isSubmitting} className="self-start">
        {form.formState.isSubmitting ? "Saving…" : "Save correction"}
      </Button>
    </form>
  );
}
