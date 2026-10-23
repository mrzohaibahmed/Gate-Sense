import { useQuery, useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, CircleAlert, ImagePlus, X } from "lucide-react";
import { useRef, useState } from "react";
import { Link } from "react-router";
import { toast } from "sonner";

import { ApiError, api } from "@/api/client";
import type { PhotoResult } from "@/api/types";
import { PageHeader } from "@/app/AppShell";
import { Button } from "@/components/ui/button";
import { Badge, Card, CardContent } from "@/components/ui/card";
import { Field, NativeSelect } from "@/components/ui/input";
import { type ImagePreparer, shrinkImage } from "@/lib/images";
import { cn } from "@/lib/utils";

/** Same limit as the API (MAX_UPLOAD_PHOTOS). */
export const MAX_PHOTOS = 10;
type Picked = { id: number; name: string; image: string | null; error: string | null };

/** Enrollment from photo files: the same checks as the camera (check_photo); the passing photos are saved. */
export function UploadPhotos({
  preselected,
  prepare = shrinkImage,
  modes,
}: {
  preselected: number | null;
  prepare?: ImagePreparer;
  modes?: React.ReactNode;
}) {
  const queryClient = useQueryClient();
  const [personId, setPersonId] = useState(preselected ? String(preselected) : "");
  const [picked, setPicked] = useState<Picked[]>([]);
  const [results, setResults] = useState<{ rows: PhotoResult[]; embeddings: number } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [dragging, setDragging] = useState(false);
  const nextId = useRef(1);
  const input = useRef<HTMLInputElement>(null);
  const people = useQuery({
    queryKey: ["people", "enrollment"],
    queryFn: () => api.get("/api/people", { query: { active: true, page_size: 500, sort: "name" } }),
  });
  const person = people.data?.items.find((p) => String(p.id) === personId);

  const add = (files: File[]) => {
    setResults(null);
    setError(null);
    const room = MAX_PHOTOS - picked.length;
    const images = files.filter((f) => f.type === "" || f.type.startsWith("image/"));
    if (images.length < files.length) setError("Only image files (JPEG or PNG) can be added.");
    if (images.length > room) setError(`At most ${MAX_PHOTOS} photos at a time; the rest were left out.`);
    for (const file of images.slice(0, Math.max(0, room))) {
      const id = nextId.current++;
      setPicked((list) => [...list, { id, name: file.name, image: null, error: null }]);
      prepare(file).then(
        (image) => setPicked((list) => list.map((p) => (p.id === id ? { ...p, image } : p))),
        (e: unknown) =>
          setPicked((list) =>
            list.map((p) => (p.id === id ? { ...p, error: e instanceof Error ? e.message : String(e) } : p)),
          ),
      );
    }
  };

  const ready = picked.filter((p) => p.image);
  const preparing = picked.some((p) => !p.image && !p.error);
  const upload = async () => {
    if (!person) return;
    setBusy(true);
    setError(null);
    try {
      const r = await api.post("/api/people/{person_id}/photos", {
        path: { person_id: person.id },
        body: { photos: ready.map((p) => ({ name: p.name, image: p.image! })) },
      });
      setResults({ rows: r.results, embeddings: r.embeddings });
      setPicked([]);
      if (r.saved) toast.success(`${r.saved} of ${r.results.length} photo(s) saved for ${person.name}`);
      else toast.error("No photo passed the checks");
      for (const key of [["person"], ["people"]]) void queryClient.invalidateQueries({ queryKey: key });
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <PageHeader title="Enrollment">{modes}</PageHeader>
      <div className="flex flex-col gap-4 px-4 pb-4">
        <Card>
          <CardContent className="flex flex-col gap-4">
            <Field label="Person" htmlFor="upload-person" className="max-w-md">
              <NativeSelect
                id="upload-person"
                value={personId}
                onChange={(e) => {
                  setPersonId(e.target.value);
                  setResults(null);
                }}
              >
                <option value="">Choose…</option>
                {people.data?.items.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.name} ({p.employee_code}){p.photos ? ` · ${p.photos} photos` : ""}
                  </option>
                ))}
              </NativeSelect>
            </Field>
            <div
              role="group"
              aria-label="Drop photos here"
              onDragOver={(e) => {
                e.preventDefault();
                setDragging(true);
              }}
              onDragLeave={() => setDragging(false)}
              onDrop={(e) => {
                e.preventDefault();
                setDragging(false);
                add(Array.from(e.dataTransfer.files));
              }}
              className={cn(
                "flex flex-col items-center gap-2 rounded-lg border-2 border-dashed p-6 text-center text-sm text-muted-foreground",
                dragging && "border-primary bg-accent",
              )}
            >
              <ImagePlus className="size-6" />
              <p>
                Drop up to {MAX_PHOTOS} photos here, or{" "}
                <button
                  type="button"
                  className="font-medium text-primary underline"
                  onClick={() => input.current?.click()}
                >
                  choose files
                </button>
                .
              </p>
              <p className="text-xs">
                One face per photo, looking at the camera, well lit. Each photo gets the same checks as the camera.
              </p>
              <input
                ref={input}
                type="file"
                accept="image/jpeg,image/png"
                multiple
                hidden
                aria-label="Photo files"
                onChange={(e) => {
                  add(Array.from(e.target.files ?? []));
                  e.target.value = "";
                }}
              />
            </div>
            {picked.length > 0 && (
              <ul className="grid grid-cols-[repeat(auto-fill,minmax(7rem,1fr))] gap-2" aria-label="Picked photos">
                {picked.map((p) => (
                  <li key={p.id} className="flex flex-col gap-1 rounded-md border p-1 text-xs">
                    {p.image ? (
                      <img src={p.image} alt={p.name} className="aspect-square w-full rounded object-cover" />
                    ) : (
                      <span className="flex aspect-square items-center justify-center rounded bg-muted text-muted-foreground">
                        {p.error ? "unreadable" : "…"}
                      </span>
                    )}
                    <span className="flex items-center gap-1">
                      <span className="truncate" title={p.name}>
                        {p.name}
                      </span>
                      <button
                        type="button"
                        className="ml-auto rounded p-0.5 hover:bg-muted"
                        aria-label={`Remove ${p.name}`}
                        onClick={() => setPicked((list) => list.filter((x) => x.id !== p.id))}
                      >
                        <X className="size-3.5" />
                      </button>
                    </span>
                    {p.error && <span className="text-destructive">{p.error}</span>}
                  </li>
                ))}
              </ul>
            )}
            {error && (
              <p role="alert" className="text-sm text-destructive">
                {error}
              </p>
            )}
            <Button
              className="self-start"
              disabled={!person || ready.length === 0 || preparing || busy}
              onClick={() => void upload()}
            >
              {busy ? "Checking…" : `Check and save ${ready.length || ""} photo${ready.length === 1 ? "" : "s"}`}
            </Button>
          </CardContent>
        </Card>
        {results && person && (
          <Card>
            <CardContent className="flex flex-col gap-2">
              <p className="text-sm font-medium">
                {results.rows.filter((r) => r.ok).length} of {results.rows.length} saved · {person.name} now has{" "}
                {results.embeddings} face embedding(s).{" "}
                <Link to={`/people/${person.id}`} className="text-primary underline">
                  Open the profile
                </Link>
              </p>
              <ul className="flex flex-col gap-1 text-sm" aria-label="Results">
                {results.rows.map((r, i) => (
                  <li key={`${r.name}-${i}`} className="flex flex-wrap items-center gap-2">
                    {r.ok ? (
                      <CheckCircle2 className="size-4 text-ok" aria-hidden />
                    ) : (
                      <CircleAlert className="size-4 text-destructive" aria-hidden />
                    )}
                    <span className="font-medium">{r.name}</span>
                    {r.ok ? (
                      <Badge tone="ok">Saved{r.saved_as ? ` as ${r.saved_as}` : ""}</Badge>
                    ) : (
                      <Badge tone="error">Refused</Badge>
                    )}
                    {!r.ok && <span className="text-muted-foreground">{r.message}</span>}
                    {r.score != null && (
                      <span className="text-xs text-muted-foreground">score {Math.round(r.score)}</span>
                    )}
                  </li>
                ))}
              </ul>
            </CardContent>
          </Card>
        )}
      </div>
    </>
  );
}
