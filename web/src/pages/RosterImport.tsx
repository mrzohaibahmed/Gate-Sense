import { useQueryClient } from "@tanstack/react-query";
import { Download, FileSpreadsheet, Upload } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";

import { ApiError, postImage, resourceUrl } from "@/api/client";
import type { RosterImportPreview } from "@/api/types";
import { useFilters } from "@/app/filters";
import { Badge } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";
import { Field, Input } from "@/components/ui/input";
import { Td, Th } from "@/components/ui/misc";

/** The file as the request body, with a content type the API's docs list. */
function asBody(file: File): Blob {
  const xlsx = file.name.toLowerCase().endsWith(".xlsx");
  return new Blob([file], {
    type: xlsx ? "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" : "text/csv",
  });
}

/**
 * Roster > Import from Excel: one row per person (employee code), one column per date (YYYY-MM-DD), each cell a
 * shift's name, OFF, or blank (leave that day as it is). The file is checked first: every change and every error
 * (line and column) is listed; it is imported with a reason only when there is no error, all cells or none.
 */
export function RosterImportDialog({
  open,
  onOpenChange,
  first,
  last,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  first: string;
  last: string;
}) {
  const queryClient = useQueryClient();
  const { siteId } = useFilters();
  const [file, setFile] = useState<File | null>(null);
  const [preview, setPreview] = useState<RosterImportPreview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);

  const reset = () => {
    setFile(null);
    setPreview(null);
    setError(null);
    setReason("");
  };
  const close = (o: boolean) => {
    if (!o) reset();
    onOpenChange(o);
  };
  const choose = async (chosen: File | null) => {
    reset();
    if (!chosen) return;
    setFile(chosen);
    setBusy(true);
    try {
      setPreview(await postImage("/api/roster/import/preview", asBody(chosen), { filename: chosen.name }));
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };
  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!file || !preview) return;
    if (!reason.trim()) {
      setError("A reason is required (it goes to the audit log).");
      return;
    }
    setBusy(true);
    try {
      const done = await postImage("/api/roster/import", asBody(file), { filename: file.name, reason });
      toast.success(`Imported ${file.name}: ${done.changed} roster entries set.`);
      for (const k of [["roster"], ["attendance"], ["overview"], ["reports"], ["shifts"]]) {
        void queryClient.invalidateQueries({ queryKey: k });
      }
      close(false);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };
  const template = (format: "xlsx" | "csv") =>
    resourceUrl("/api/roster/import/template", undefined, { format, from: first, to: last, site_id: siteId });

  const counts = preview?.counts;
  return (
    <Dialog open={open} onOpenChange={close}>
      <DialogContent className="max-w-4xl">
        <form onSubmit={submit} className="flex flex-col gap-4">
          <div className="flex flex-col gap-1">
            <DialogTitle>Import the roster from a file</DialogTitle>
            <DialogDescription>
              An Excel (.xlsx) or CSV file: an Employee code column, then one column per date written as YYYY-MM-DD.
              Each cell is a shift&apos;s name or OFF; a blank cell leaves that day as it is. Download this week as a
              file to start from.
            </DialogDescription>
          </div>
          <div className="flex flex-wrap items-end gap-2">
            <Field label="File" htmlFor="roster-file" className="min-w-64 flex-1">
              <Input
                id="roster-file"
                type="file"
                accept=".xlsx,.csv,text/csv,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                onChange={(e) => void choose(e.target.files?.[0] ?? null)}
              />
            </Field>
            <Button variant="outline" size="sm" asChild>
              <a href={template("xlsx")} download>
                <Download /> This week (Excel)
              </a>
            </Button>
            <Button variant="outline" size="sm" asChild>
              <a href={template("csv")} download>
                <Download /> This week (CSV)
              </a>
            </Button>
          </div>

          {busy && !preview && <p className="text-sm text-muted-foreground">Checking the file…</p>}
          {preview && counts && (
            <>
              <div className="flex flex-wrap items-center gap-2 text-sm" aria-label="Summary">
                <FileSpreadsheet className="size-4 text-muted-foreground" aria-hidden />
                <span className="font-medium">{preview.file}</span>
                <span className="text-muted-foreground">
                  {preview.dates[0]} to {preview.dates.at(-1)}, {preview.people} people
                </span>
                <Badge tone="warn">{counts.set} to set</Badge>
                <Badge>{counts.unchanged} unchanged</Badge>
                {counts.error > 0 && <Badge tone="error">{counts.error} errors</Badge>}
              </div>
              {preview.ignored_columns.length > 0 && (
                <p className="text-xs text-muted-foreground">
                  Not imported (unknown columns): {preview.ignored_columns.join(", ")}
                </p>
              )}
              {preview.errors.length > 0 && (
                <div className="max-h-48 overflow-auto rounded-md border border-destructive/40">
                  <table className="w-full text-sm" aria-label="Errors">
                    <thead>
                      <tr>
                        <Th>Line</Th>
                        <Th>Column</Th>
                        <Th>Code</Th>
                        <Th>Problem</Th>
                      </tr>
                    </thead>
                    <tbody>
                      {preview.errors.map((e) => (
                        <tr key={`${e.line}-${e.column}`}>
                          <Td className="tabular-nums">{e.line}</Td>
                          <Td>{e.column || "(row)"}</Td>
                          <Td>{e.employee_code}</Td>
                          <Td className="text-destructive">{e.message}</Td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
              {preview.changes.length > 0 && (
                <div className="max-h-64 overflow-auto rounded-md border">
                  <table className="w-full text-sm" aria-label="Changes">
                    <thead>
                      <tr>
                        <Th>Line</Th>
                        <Th>Person</Th>
                        <Th>Date</Th>
                        <Th>Now</Th>
                        <Th>New</Th>
                      </tr>
                    </thead>
                    <tbody>
                      {preview.changes.map((c) => (
                        <tr key={`${c.line}-${c.date}`}>
                          <Td className="tabular-nums">{c.line}</Td>
                          <Td>
                            {c.name} <span className="text-muted-foreground">{c.employee_code}</span>
                          </Td>
                          <Td className="tabular-nums">{c.date}</Td>
                          <Td className="text-muted-foreground">{c.old ?? "—"}</Td>
                          <Td className="font-medium">{c.new}</Td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
              {counts.error > 0 ? (
                <p role="alert" className="text-sm text-destructive">
                  Fix the errors in the file and choose it again: nothing is imported while any cell has an error.
                </p>
              ) : counts.set === 0 ? (
                <p className="text-sm text-muted-foreground">The roster already matches the file.</p>
              ) : (
                <Field label="Reason (required)" htmlFor="roster-import-reason">
                  <Input id="roster-import-reason" value={reason} onChange={(e) => setReason(e.target.value)} />
                </Field>
              )}
            </>
          )}
          {error && (
            <p role="alert" className="text-sm text-destructive">
              {error}
            </p>
          )}
          <div className="flex justify-end gap-2">
            <Button type="button" variant="outline" onClick={() => close(false)}>
              Cancel
            </Button>
            <Button type="submit" disabled={busy || !preview || !counts || counts.error > 0 || counts.set === 0}>
              <Upload /> {counts?.set ? `Import ${counts.set} ${counts.set === 1 ? "entry" : "entries"}` : "Import"}
            </Button>
          </div>
        </form>
      </DialogContent>
    </Dialog>
  );
}
