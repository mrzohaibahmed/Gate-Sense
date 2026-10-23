import { zodResolver } from "@hookform/resolvers/zod";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Camera as CameraIcon, CheckCircle2, CircleAlert, RotateCcw, UserPlus, Users } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { useForm } from "react-hook-form";
import { Link, useSearchParams } from "react-router";
import { z } from "zod";

import { ApiError, api, postImage } from "@/api/client";
import type { FrameCheck, Person, Review, StepInfo } from "@/api/types";
import { PageHeader } from "@/app/AppShell";
import { Badge, Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Field, Input, NativeSelect, Textarea } from "@/components/ui/input";
import { ErrorState } from "@/components/ui/misc";
import { cn } from "@/lib/utils";

import { UploadPhotos } from "./UploadPhotos";

// --- The camera, behind a small interface so tests can replace it --------------------------------

export type CameraAdapter = {
  /** Start the webcam into this <video>; reject with a readable message if it can't. */
  start: (video: HTMLVideoElement) => Promise<void>;
  stop: () => void;
  /** The current raw (not mirrored) frame as a JPEG. */
  grab: (video: HTMLVideoElement, quality: number) => Promise<Blob | null>;
};

export function browserCamera(): CameraAdapter {
  let stream: MediaStream | null = null;
  return {
    async start(video) {
      if (!navigator.mediaDevices?.getUserMedia) {
        throw new Error("This browser can't use a camera here (it needs https or localhost).");
      }
      try {
        stream = await navigator.mediaDevices.getUserMedia({
          video: { width: { ideal: 1280 }, height: { ideal: 720 }, facingMode: "user" },
          audio: false,
        });
      } catch (e) {
        const name = (e as { name?: string }).name;
        throw new Error(
          name === "NotAllowedError"
            ? "The browser wasn't allowed to use the camera. Allow it in the address bar and try again."
            : name === "NotReadableError"
              ? "The camera is busy: another app (or a camera worker) is using it."
              : "No camera found.",
          { cause: e },
        );
      }
      video.srcObject = stream;
      await video.play();
    },
    stop() {
      stream?.getTracks().forEach((t) => t.stop());
      stream = null;
    },
    grab(video, quality) {
      const canvas = document.createElement("canvas");
      canvas.width = video.videoWidth;
      canvas.height = video.videoHeight;
      const ctx = canvas.getContext("2d");
      if (!ctx || !canvas.width) return Promise.resolve(null);
      ctx.drawImage(video, 0, 0); // raw frame: the preview is mirrored with CSS only
      return new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", quality));
    },
  };
}

function toDataUrl(blob: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(blob);
  });
}

// --- The wizard ------------------------------------------------------------------------------------

type Captures = Record<string, string>; // step key -> JPEG data URL

export function EnrollmentPage() {
  const [params] = useSearchParams();
  const person = params.get("person");
  return params.get("tab") === "upload" ? (
    <UploadPhotos preselected={person ? Number(person) : null} modes={<EnrollmentModes upload />} />
  ) : (
    <EnrollmentWizard />
  );
}

/** Switch between the guided camera and photo files (keeps the chosen person). */
function EnrollmentModes({ upload = false }: { upload?: boolean }) {
  const [params] = useSearchParams();
  const person = params.get("person");
  const query = (tab?: string) => {
    const q = new URLSearchParams();
    if (person) q.set("person", person);
    if (tab) q.set("tab", tab);
    const text = q.toString();
    return text ? `?${text}` : "";
  };
  return (
    <div role="tablist" aria-label="Enrollment mode" className="flex gap-1 rounded-md bg-muted p-1 text-sm">
      <Link
        role="tab"
        aria-selected={!upload}
        to={`/enrollment${query()}`}
        className={cn("rounded px-2 py-1", !upload && "bg-background font-medium shadow-sm")}
      >
        Camera
      </Link>
      <Link
        role="tab"
        aria-selected={upload}
        to={`/enrollment${query("upload")}`}
        className={cn("rounded px-2 py-1", upload && "bg-background font-medium shadow-sm")}
      >
        Photo files
      </Link>
    </div>
  );
}

export function EnrollmentWizard({ camera, checkEveryMs = 300 }: { camera?: CameraAdapter; checkEveryMs?: number }) {
  const [params] = useSearchParams();
  const [person, setPerson] = useState<Person | null>(null);
  const [stage, setStage] = useState<"details" | "capture" | "review" | "done">("details");
  const [stepIndex, setStepIndex] = useState(0);
  const [captures, setCaptures] = useState<Captures>({});
  const steps = useQuery({
    queryKey: ["enrollment", "steps"],
    queryFn: () => api.get("/api/enrollment/steps"),
    staleTime: Infinity,
  });
  const required = (steps.data ?? []).filter((s) => s.required);
  const preselected = params.get("person");

  const labels = ["Details", ...required.map((s) => s.title), "Review and save"];
  const current = stage === "details" ? 0 : stage === "capture" ? 1 + stepIndex : labels.length - 1;

  return (
    <>
      <PageHeader title="Enrollment">
        <EnrollmentModes />
      </PageHeader>
      <div className="flex flex-col gap-4 px-4 pb-4">
        <ol className="flex flex-wrap gap-2 text-sm" aria-label="Steps">
          {labels.map((label, i) => (
            <li
              key={label}
              aria-current={i === current ? "step" : undefined}
              className={cn(
                "flex items-center gap-1 rounded-md border px-2 py-1",
                i === current && "border-primary bg-accent font-medium text-accent-foreground",
                i < current && "text-muted-foreground",
              )}
            >
              <span className="flex size-5 items-center justify-center rounded-full bg-muted text-xs">{i + 1}</span>
              {label}
            </li>
          ))}
        </ol>
        {steps.error ? (
          <ErrorState error={steps.error} retry={() => void steps.refetch()} />
        ) : stage === "details" ? (
          <DetailsStep
            preselected={preselected ? Number(preselected) : null}
            onNext={(p) => {
              setPerson(p);
              setCaptures({});
              setStepIndex(0);
              setStage("capture");
            }}
          />
        ) : stage === "capture" && person && required[stepIndex] ? (
          <CaptureStep
            person={person}
            step={required[stepIndex]!}
            position={[stepIndex + 1, required.length]}
            captured={captures}
            camera={camera}
            checkEveryMs={checkEveryMs}
            onCapture={(dataUrl) => {
              const next = { ...captures, [required[stepIndex]!.key]: dataUrl };
              setCaptures(next);
              const missing = required.findIndex((s) => !next[s.key]);
              if (missing === -1) setStage("review");
              else setStepIndex(missing);
            }}
            onBack={() => (stepIndex === 0 ? setStage("details") : setStepIndex(stepIndex - 1))}
          />
        ) : stage === "review" && person ? (
          <ReviewStep
            person={person}
            steps={required}
            captures={captures}
            onRetake={(key) => {
              setCaptures(({ [key]: _gone, ...rest }) => rest);
              setStepIndex(required.findIndex((s) => s.key === key));
              setStage("capture");
            }}
            onSaved={() => setStage("done")}
          />
        ) : stage === "done" && person ? (
          <Card>
            <CardContent className="flex flex-col items-start gap-2">
              <p className="flex items-center gap-2 font-medium">
                <CheckCircle2 className="size-5 text-ok" /> {person.name} is enrolled.
              </p>
              <p className="text-sm text-muted-foreground">Running cameras recognise them within a few seconds.</p>
              <div className="flex gap-2">
                <Button asChild variant="outline">
                  <Link to={`/people/${person.id}`}>Open the profile</Link>
                </Button>
                <Button
                  onClick={() => {
                    setPerson(null);
                    setStage("details");
                  }}
                >
                  Enroll someone else
                </Button>
              </div>
            </CardContent>
          </Card>
        ) : (
          <Card className="h-64" />
        )}
      </div>
    </>
  );
}

// --- Step 1: who --------------------------------------------------------------------------------------

const newPersonSchema = z.object({
  employee_code: z
    .string()
    .trim()
    .regex(/^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$/, "Letters, digits, '_', '-' or '.' (it is also the photo folder)"),
  name: z.string().trim().min(1, "Enter the name"),
  department: z.string().trim(),
  shift: z.string().trim(),
  reason: z.string().trim().min(1, "A reason is required (it goes to the audit log)"),
});
type NewPerson = z.infer<typeof newPersonSchema>;

function DetailsStep({ preselected, onNext }: { preselected: number | null; onNext: (p: Person) => void }) {
  const [mode, setMode] = useState<"existing" | "new">(preselected ? "existing" : "new");
  const [chosen, setChosen] = useState<string>(preselected ? String(preselected) : "");
  const [error, setError] = useState<string | null>(null);
  const queryClient = useQueryClient();
  const people = useQuery({
    queryKey: ["people", "enrollment"],
    queryFn: () => api.get("/api/people", { query: { active: true, page_size: 500, sort: "name" } }),
  });
  const form = useForm<NewPerson>({
    resolver: zodResolver(newPersonSchema),
    defaultValues: { employee_code: "", name: "", department: "", shift: "", reason: "new starter" },
  });
  const errors = form.formState.errors;
  const create = form.handleSubmit(async (v) => {
    setError(null);
    try {
      const p = await api.post("/api/people", {
        body: {
          employee_code: v.employee_code,
          name: v.name,
          department: v.department || null,
          shift: v.shift || null,
          reason: v.reason,
        },
      });
      void queryClient.invalidateQueries({ queryKey: ["people"] });
      onNext(p);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    }
  });
  return (
    <Card>
      <CardContent className="flex flex-col gap-4">
        <div role="radiogroup" aria-label="Who" className="flex gap-2">
          <Button
            variant={mode === "new" ? "default" : "outline"}
            onClick={() => setMode("new")}
            role="radio"
            aria-checked={mode === "new"}
          >
            <UserPlus /> A new person
          </Button>
          <Button
            variant={mode === "existing" ? "default" : "outline"}
            onClick={() => setMode("existing")}
            role="radio"
            aria-checked={mode === "existing"}
          >
            <Users /> Someone already added
          </Button>
        </div>
        {mode === "existing" ? (
          <div className="flex max-w-md flex-col gap-2">
            <Field label="Person" htmlFor="existing">
              <NativeSelect id="existing" value={chosen} onChange={(e) => setChosen(e.target.value)}>
                <option value="">Choose…</option>
                {people.data?.items.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.name} ({p.employee_code}){p.embeddings ? ` · ${p.embeddings} photos enrolled` : ""}
                  </option>
                ))}
              </NativeSelect>
            </Field>
            <Button
              className="self-start"
              disabled={!chosen}
              onClick={() => {
                const p = people.data?.items.find((x) => String(x.id) === chosen);
                if (p) onNext(p);
              }}
            >
              Next: photos
            </Button>
          </div>
        ) : (
          <form onSubmit={create} className="grid max-w-2xl gap-4 sm:grid-cols-2" noValidate aria-label="New person">
            <Field label="Employee code" htmlFor="code" error={errors.employee_code?.message}>
              <Input id="code" aria-invalid={!!errors.employee_code} {...form.register("employee_code")} />
            </Field>
            <Field label="Name" htmlFor="name" error={errors.name?.message}>
              <Input id="name" aria-invalid={!!errors.name} {...form.register("name")} />
            </Field>
            <Field label="Department" htmlFor="department">
              <Input id="department" {...form.register("department")} />
            </Field>
            <Field label="Shift" htmlFor="shift">
              <Input id="shift" {...form.register("shift")} />
            </Field>
            <Field label="Reason" htmlFor="new-reason" error={errors.reason?.message} className="sm:col-span-2">
              <Textarea id="new-reason" aria-invalid={!!errors.reason} {...form.register("reason")} />
            </Field>
            {error && (
              <p role="alert" className="text-sm text-destructive sm:col-span-2">
                {error}
              </p>
            )}
            <Button type="submit" className="self-start" disabled={form.formState.isSubmitting}>
              Add and continue
            </Button>
          </form>
        )}
      </CardContent>
    </Card>
  );
}

// --- Steps 2-4: guided capture ------------------------------------------------------------------------

function CaptureStep({
  person,
  step,
  position,
  captured,
  camera,
  checkEveryMs,
  onCapture,
  onBack,
}: {
  person: Person;
  step: StepInfo;
  position: [number, number];
  captured: Captures;
  camera?: CameraAdapter;
  checkEveryMs: number;
  onCapture: (dataUrl: string) => void;
  onBack: () => void;
}) {
  const video = useRef<HTMLVideoElement>(null);
  const adapter = useRef<CameraAdapter>(camera ?? browserCamera());
  const [cameraError, setCameraError] = useState<string | null>(null);
  const [started, setStarted] = useState(false);
  // The latest answer, kept with the step it was for (a new step starts without one).
  const [latest, setLatest] = useState<(FrameCheck & { step: string }) | null>(null);
  const check = latest?.step === step.key ? latest : null;
  const [checkError, setCheckError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const inFlight = useRef(false);

  useEffect(() => {
    const cam = adapter.current;
    let cancelled = false;
    if (video.current) {
      cam
        .start(video.current)
        .then(() => !cancelled && setStarted(true))
        .catch((e: Error) => !cancelled && setCameraError(e.message));
    }
    return () => {
      cancelled = true;
      cam.stop();
    };
  }, []);

  // A frame to the API every `checkEveryMs`, never two at once.
  useEffect(() => {
    if (!started) return;
    const timer = setInterval(async () => {
      if (inFlight.current || !video.current) return;
      inFlight.current = true;
      try {
        const blob = await adapter.current.grab(video.current, 0.8);
        if (blob) {
          const result = await postImage("/api/enrollment/check", blob, { step: step.key });
          setLatest({ ...result, step: step.key });
          setCheckError(null);
        }
      } catch (e) {
        setLatest(null); // no Capture on a stale answer
        setCheckError(e instanceof ApiError ? e.message : String(e));
      } finally {
        inFlight.current = false;
      }
    }, checkEveryMs);
    return () => clearInterval(timer);
  }, [started, step.key, checkEveryMs]);

  const ok = !!check?.ok;

  const capture = useCallback(async () => {
    if (!video.current) return;
    setBusy(true);
    try {
      const blob = await adapter.current.grab(video.current, 0.92);
      if (blob) onCapture(await toDataUrl(blob));
    } finally {
      setBusy(false);
    }
  }, [onCapture]);

  // Boxes come from the raw frame; the preview is mirrored, so mirror x too.
  const mirrorBox = (b: number[] | null | undefined) =>
    b
      ? {
          left: `${(1 - b[2]!) * 100}%`,
          top: `${b[1]! * 100}%`,
          width: `${(b[2]! - b[0]!) * 100}%`,
          height: `${(b[3]! - b[1]!) * 100}%`,
        }
      : null;
  const guide = mirrorBox(check?.guide);
  const face = mirrorBox(check?.box);

  return (
    <div className="grid gap-4 lg:grid-cols-3">
      <Card className="overflow-hidden lg:col-span-2">
        <div className="relative bg-black">
          <video
            ref={video}
            muted
            playsInline
            aria-label="Camera preview"
            className="aspect-video w-full -scale-x-100 object-contain"
          />
          {guide && (
            <div
              data-testid="guide-box"
              data-state={ok ? "good" : (check?.state ?? "none")}
              className={cn(
                "absolute rounded-[40%] border-4 transition-colors",
                ok ? "border-ok" : check?.state === "bad" ? "border-destructive/80" : "border-white/60",
              )}
              style={guide}
            />
          )}
          {face && <div className="absolute border-2 border-white/70" style={face} aria-hidden />}
          {cameraError && (
            <div
              role="alert"
              className="absolute inset-0 flex items-center justify-center p-4 text-center text-sm text-white"
            >
              {cameraError}
            </div>
          )}
        </div>
      </Card>
      <Card>
        <CardContent className="flex flex-col gap-4">
          <div>
            <p className="text-xs text-muted-foreground">
              {person.name} · step {position[0]} of {position[1]}
            </p>
            <h2 className="text-base font-semibold">{step.title}</h2>
            <p className="text-sm">{step.instruction}</p>
          </div>
          <p
            role="status"
            className={cn(
              "rounded-md px-2 py-1.5 text-sm font-medium",
              ok
                ? "bg-ok/10 text-ok"
                : check?.state === "bad"
                  ? "bg-destructive/10 text-destructive"
                  : "bg-muted text-muted-foreground",
            )}
          >
            {cameraError
              ? "Camera not available"
              : !started
                ? "Starting the camera…"
                : (check?.message ?? "Look at the camera")}
          </p>
          {check?.yaw != null && (
            <p className="text-xs text-muted-foreground">
              turned {Math.abs(check.yaw).toFixed(0)}° {check.yaw >= 0 ? "left" : "right"} · tilted{" "}
              {Math.abs(check.pitch ?? 0).toFixed(0)}°
            </p>
          )}
          {checkError && <p className="text-xs text-destructive">{checkError}</p>}
          <div className="flex gap-2">
            <Button variant="outline" onClick={onBack}>
              Back
            </Button>
            <Button onClick={() => void capture()} disabled={!ok || busy} aria-label="Capture">
              <CameraIcon /> Capture
            </Button>
          </div>
          {Object.keys(captured).length > 0 && (
            <ul className="flex gap-2" aria-label="Taken">
              {Object.entries(captured).map(([key, url]) => (
                <li key={key} className="flex flex-col items-center gap-1 text-xs">
                  <img src={url} alt={`${key} photo`} className="h-12 w-16 -scale-x-100 rounded object-cover" />
                  {key}
                </li>
              ))}
            </ul>
          )}
        </CardContent>
      </Card>
    </div>
  );
}

// --- Last step: review and save ------------------------------------------------------------------------

function ReviewStep({
  person,
  steps,
  captures,
  onRetake,
  onSaved,
}: {
  person: Person;
  steps: StepInfo[];
  captures: Captures;
  onRetake: (key: string) => void;
  onSaved: () => void;
}) {
  const queryClient = useQueryClient();
  const photos = steps.filter((s) => captures[s.key]).map((s) => ({ step: s.key, image: captures[s.key]! }));
  const review = useQuery<Review>({
    queryKey: ["enrollment", "review", person.id, photos.map((p) => p.image.length).join(",")],
    queryFn: () => api.post("/api/enrollment/review", { query: { person_id: person.id }, body: { photos } }),
    gcTime: 0,
    staleTime: Infinity,
  });
  const [reason, setReason] = useState("enrolled on the dashboard");
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const save = async () => {
    if (!reason.trim()) {
      setError("A reason is required (it goes to the audit log).");
      return;
    }
    setSaving(true);
    setError(null);
    try {
      await api.post("/api/people/{person_id}/enroll", { path: { person_id: person.id }, body: { photos, reason } });
      for (const key of [["people"], ["person"]]) void queryClient.invalidateQueries({ queryKey: key });
      onSaved();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  };
  const byStep = new Map((review.data?.photos ?? []).map((r) => [r.step, r]));
  return (
    <Card>
      <CardContent className="flex flex-col gap-4">
        <h2 className="text-base font-semibold">Review {person.name}'s photos</h2>
        {review.error && <ErrorState error={review.error} retry={() => void review.refetch()} />}
        <ul className="grid gap-4 sm:grid-cols-3">
          {steps.map((s) => {
            const r = byStep.get(s.key);
            return (
              <li key={s.key} className="flex flex-col gap-2 rounded-md border p-2">
                {captures[s.key] && (
                  <img
                    src={captures[s.key]}
                    alt={`${s.title} photo`}
                    className="aspect-video w-full -scale-x-100 rounded object-cover"
                  />
                )}
                <div className="flex items-center gap-2 text-sm">
                  <span className="font-medium">{s.title}</span>
                  {review.isPending ? (
                    <Badge>checking…</Badge>
                  ) : r?.ok ? (
                    <Badge tone="ok">ok{r.score != null ? ` · ${Math.round(r.score)}` : ""}</Badge>
                  ) : (
                    <Badge tone="error">retake</Badge>
                  )}
                </div>
                {r?.problem && <p className="text-xs text-destructive">{r.problem}</p>}
                {r?.warnings.map((w) => (
                  <p key={w} className="flex gap-1 text-xs text-warn">
                    <CircleAlert className="size-3.5 shrink-0" /> {w}
                  </p>
                ))}
                <Button variant="outline" size="sm" className="self-start" onClick={() => onRetake(s.key)}>
                  <RotateCcw /> Retake
                </Button>
              </li>
            );
          })}
        </ul>
        <Field label="Reason (required)" htmlFor="enroll-reason" error={error ?? undefined} className="max-w-md">
          <Input id="enroll-reason" value={reason} onChange={(e) => setReason(e.target.value)} />
        </Field>
        <Button
          className="self-start"
          onClick={() => void save()}
          disabled={saving || !review.data?.ready || review.data.photos.some((p) => !p.ok)}
        >
          {saving ? "Saving…" : "Save enrollment"}
        </Button>
      </CardContent>
    </Card>
  );
}
