import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { api } from "@/api/client";
import type { PersonDetail } from "@/api/types";
import { ReasonDialog } from "@/components/ReasonDialog";
import { Field, Input, NativeSelect } from "@/components/ui/input";

/** Edit name, department, shift and site; the employee code is the photo folder name and can't change. */
export function EditPersonDialog({
  person,
  open,
  onOpenChange,
  onSaved,
}: {
  person: PersonDetail;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onSaved: () => void;
}) {
  const [name, setName] = useState(person.name);
  const [department, setDepartment] = useState(person.department ?? "");
  const [shift, setShift] = useState(person.shift ?? "");
  const [siteId, setSiteId] = useState(person.site_id ? String(person.site_id) : "");
  const sites = useQuery({ queryKey: ["sites"], queryFn: () => api.get("/api/sites", { query: { page_size: 500 } }) });
  return (
    <ReasonDialog
      open={open}
      onOpenChange={(o) => {
        if (o) {
          setName(person.name);
          setDepartment(person.department ?? "");
          setShift(person.shift ?? "");
          setSiteId(person.site_id ? String(person.site_id) : "");
        }
        onOpenChange(o);
      }}
      title={`Edit ${person.name}`}
      confirmLabel="Save"
      onConfirm={async (reason) => {
        if (!name.trim()) throw new Error("The name can't be empty.");
        await api.patch("/api/people/{person_id}", {
          path: { person_id: person.id },
          body: {
            name: name.trim(),
            department: department.trim() || null,
            shift: shift.trim() || null,
            ...(siteId ? { site_id: Number(siteId) } : {}),
            reason,
          },
        });
        onSaved();
      }}
    >
      <Field label="Employee code" htmlFor="edit-code" hint="It names the photo folder, so it can't be changed.">
        <Input id="edit-code" value={person.employee_code} readOnly disabled />
      </Field>
      <Field label="Name" htmlFor="edit-name">
        <Input id="edit-name" value={name} onChange={(e) => setName(e.target.value)} />
      </Field>
      <div className="grid grid-cols-2 gap-2">
        <Field label="Department" htmlFor="edit-department">
          <Input id="edit-department" value={department} onChange={(e) => setDepartment(e.target.value)} />
        </Field>
        <Field label="Shift" htmlFor="edit-shift">
          <Input id="edit-shift" value={shift} onChange={(e) => setShift(e.target.value)} />
        </Field>
      </div>
      {(sites.data?.items.length ?? 0) > 1 && (
        <Field label="Site" htmlFor="edit-site">
          <NativeSelect id="edit-site" value={siteId} onChange={(e) => setSiteId(e.target.value)}>
            {sites.data?.items.map((s) => (
              <option key={s.id} value={s.id}>
                {s.name}
              </option>
            ))}
          </NativeSelect>
        </Field>
      )}
    </ReasonDialog>
  );
}

/** Delete one photo; the person's face data is rebuilt from the rest. */
export function DeletePhotoDialog({
  person,
  file,
  onOpenChange,
  onDone,
}: {
  person: PersonDetail;
  file: string | null;
  onOpenChange: (open: boolean) => void;
  onDone: () => void;
}) {
  return (
    <ReasonDialog
      open={file !== null}
      onOpenChange={onOpenChange}
      title={`Delete photo ${file ?? ""}?`}
      description="The photo is deleted and the face data rebuilt from the other photos."
      confirmLabel="Delete photo"
      destructive
      onConfirm={async (reason) => {
        if (!file) return;
        await api.delete("/api/people/{person_id}/photos/{file}", {
          path: { person_id: person.id, file },
          query: { reason },
        });
        onDone();
      }}
    />
  );
}

/** Delete every photo and the face data. The person and their attendance stay. */
export function DeleteFaceDataDialog({
  person,
  open,
  onOpenChange,
  onDone,
}: {
  person: PersonDetail;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onDone: (deactivated: boolean) => void;
}) {
  const [understood, setUnderstood] = useState(false);
  const [deactivate, setDeactivate] = useState(false);
  return (
    <ReasonDialog
      open={open}
      onOpenChange={(o) => {
        if (o) {
          setUnderstood(false);
          setDeactivate(false);
        }
        onOpenChange(o);
      }}
      title={`Delete ${person.name}'s face data?`}
      description={`All ${person.photos} photo(s) and the face data are deleted: the cameras stop recognising them. The person and their attendance history stay. This can't be undone.`}
      confirmLabel="Delete face data"
      destructive
      onConfirm={async (reason) => {
        if (!understood) throw new Error("Tick “I understand” first.");
        await api.post("/api/people/{person_id}/delete-face-data", {
          path: { person_id: person.id },
          body: { reason, deactivate },
        });
        onDone(deactivate);
      }}
    >
      <label className="flex items-center gap-2 text-sm">
        <input type="checkbox" checked={deactivate} onChange={(e) => setDeactivate(e.target.checked)} />
        Also deactivate {person.name} (they stop showing as absent)
      </label>
      <label className="flex items-center gap-2 text-sm">
        <input type="checkbox" checked={understood} onChange={(e) => setUnderstood(e.target.checked)} />I understand
        this can't be undone
      </label>
    </ReasonDialog>
  );
}
