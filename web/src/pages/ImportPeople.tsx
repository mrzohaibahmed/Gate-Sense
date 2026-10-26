import { useQueryClient } from "@tanstack/react-query";
import { Download, FileSpreadsheet, Upload } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";

import { ApiError, postImage, resourceUrl } from "@/api/client";
import type { ImportPreview, ImportRowOut } from "@/api/types";
import { Badge } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";
import { Field, Input } from "@/components/ui/input";
import { Switch, Td, Th } from "@/components/ui/misc";

const ACTION: Record<ImportRowOut["action"], { label: string; tone: "ok" | "warn" | "neutral" | "error" }> = {
  new: { label: "New", tone: "ok" },
  update: { label: "Update", tone: "warn" },
  unchanged: { label: "Unchanged", tone: "neutral" },
  error: { label: "Error", tone: "error" },
};
const FIELD: Record<string, string> = { name: "Name", department: "Department", shift: "Shift", site: "Site" };

/** The file as the request body, with a content type the API's docs list. */
function asBody(file: File): Blob {
  const xlsx = file.name.toLowerCase().endsWith(".xlsx");
  return new Blob([file], {
    type: xlsx ? "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" : "text/csv",
  });
}

function details(row: ImportRowOut): string {
  if (row.errors.length) return row.errors.join("; ");
  const changes = Object.entries(row.changes).map(
    ([field, [old, value]]) => `${FIELD[field] ?? field}: ${old ?? "(empty)"} → ${value ?? "(empty)"}`,
  );
  return [...changes, ...row.notes].join("; ");
}

/**
 * People > Import: an Excel or CSV file of employee code, name, department (and optionally shift, site). The file is
 * checked first and every row shown (new, update, unchanged, error); then it is imported with a reason, all rows or
 * none. The people get no photos: their faces are enrolled next.
 */
export function ImportPeopleDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (open: boolean) => void }) {
  const queryClient = useQueryClient();
  const [file, setFile] = useState<File | null>(null);
  const [preview, setPreview] = useState<ImportPreview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [showUnchanged, setShowUnchanged] = useState(false);

  const reset = () => {
    setFile(null);
    setPreview(null);
    setError(null);
    setReason("");
    setShowUnchanged(false);
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
      setPreview(await postImage("/api/people/import/preview", asBody(chosen), { filename: chosen.name }));
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
      const done = await postImage("/api/people/import", asBody(file), { filename: file.name, reason });
      toast.success(
        `Imported ${file.name}: ${done.added} added, ${done.updated} updated` +
          (done.added ? ". Enroll their faces next." : "."),
      );
      void queryClient.invalidateQueries({ queryKey: ["people"] });
      close(false);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const counts = preview?.counts;
  const todo = counts ? counts.new + counts.update : 0;
  const rows = (preview?.rows ?? []).filter((r) => showUnchanged || r.action !== "unchanged");
  return (
    <Dialog open={open} onOpenChange={close}>
      <DialogContent className="max-w-4xl">
        <form onSubmit={submit} className="flex flex-col gap-4">
          <div className="flex flex-col gap-1">
            <DialogTitle>Import people from a file</DialogTitle>
            <DialogDescription>
              An Excel (.xlsx) or CSV file with the columns Employee code, Name and Department (Shift and Site are
              optional). New codes are added; existing people get the file&apos;s values; people not in the file are
              left alone. Faces are enrolled afterwards.
            </DialogDescription>
          </div>
          <div className="flex flex-wrap items-end gap-2">
            <Field label="File" htmlFor="import-file" className="min-w-64 flex-1">
              <Input
                id="import-file"
                type="file"
                accept=".xlsx,.csv,text/csv,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                onChange={(e) => void choose(e.target.files?.[0] ?? null)}
              />
            </Field>
            <Button variant="outline" size="sm" asChild>
              <a href={resourceUrl("/api/people/import/template", undefined, { format: "xlsx" })} download>
                <Download /> Template (Excel)
              </a>
            </Button>
            <Button variant="outline" size="sm" asChild>
              <a href={resourceUrl("/api/people/import/template", undefined, { format: "csv" })} download>
                <Download /> Template (CSV)
              </a>
            </Button>
          </div>

          {busy && !preview && <p className="text-sm text-muted-foreground">Checking the file…</p>}
          {preview && counts && (
            <>
              <div className="flex flex-wrap items-center gap-2 text-sm" aria-label="Summary">
                <FileSpreadsheet className="size-4 text-muted-foreground" aria-hidden />
                <span className="font-medium">{preview.file}</span>
                <Badge tone="ok">{counts.new} new</Badge>
                <Badge tone="warn">{counts.update} to update</Badge>
                <Badge>{counts.unchanged} unchanged</Badge>
                {counts.error > 0 && <Badge tone="error">{counts.error} with errors</Badge>}
                <label className="ml-auto flex items-center gap-2 text-xs text-muted-foreground">
                  <Switch checked={showUnchanged} onCheckedChange={setShowUnchanged} aria-label="Show unchanged rows" />
                  Show unchanged
                </label>
              </div>
              {preview.ignored_columns.length > 0 && (
                <p className="text-xs text-muted-foreground">
                  Not imported (unknown columns): {preview.ignored_columns.join(", ")}
                </p>
              )}
              <div className="max-h-80 overflow-auto rounded-md border">
                <table className="w-full text-sm" aria-label="Rows of the file">
                  <thead>
                    <tr>
                      <Th>Line</Th>
                      <Th>Code</Th>
                      <Th>Name</Th>
                      <Th>Department</Th>
                      <Th>Result</Th>
                      <Th>Details</Th>
                    </tr>
                  </thead>
                  <tbody>
                    {rows.map((r) => (
                      <tr key={r.line} aria-label={`Line ${r.line}`}>
                        <Td className="tabular-nums">{r.line}</Td>
                        <Td>{r.employee_code}</Td>
                        <Td>{r.name}</Td>
                        <Td>{r.department ?? ""}</Td>
                        <Td>
                          <Badge tone={ACTION[r.action].tone}>{ACTION[r.action].label}</Badge>
                        </Td>
                        <Td className={r.errors.length ? "text-destructive" : "text-muted-foreground"}>{details(r)}</Td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              {counts.error > 0 ? (
                <p role="alert" className="text-sm text-destructive">
                  Fix the rows with errors in the file and choose it again: nothing is imported while any row has an
                  error.
                </p>
              ) : todo === 0 ? (
                <p className="text-sm text-muted-foreground">Everyone in the file is already up to date.</p>
              ) : (
                <Field label="Reason (required)" htmlFor="import-reason">
                  <Input id="import-reason" value={reason} onChange={(e) => setReason(e.target.value)} />
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
            <Button type="submit" disabled={busy || !preview || !counts || counts.error > 0 || todo === 0}>
              <Upload /> {todo ? `Import ${todo} ${todo === 1 ? "person" : "people"}` : "Import"}
            </Button>
          </div>
        </form>
      </DialogContent>
    </Dialog>
  );
}
