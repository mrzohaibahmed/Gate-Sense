import { zodResolver } from "@hookform/resolvers/zod";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Camera as CameraIcon,
  Cctv,
  CheckCircle2,
  CircleAlert,
  RotateCcw,
  UserPlus,
  Users,
  Webcam,
} from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { useForm } from "react-hook-form";
import { Link, useSearchParams } from "react-router";
import { z } from "zod";

import { ApiError, api, postImage, resourceUrl } from "@/api/client";
import type { EnrollmentSource, FrameCheck, Person, Review, StepInfo } from "@/api/types";
import { PageHeader } from "@/app/AppShell";
import { Badge, Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Field, Input, NativeSelect } from "@/components/ui/input";
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

/** Give the webcam back to the camera workers. keepalive: it still goes out while the tab is closing. */
function releaseWebcam(claimId: string): void {
  void fetch(resourceUrl("/api/enrollment/webcam/{claim_id}", { claim_id: claimId }), {
    method: "DELETE",
    credentials: "include",
    keepalive: true,
  }).catch(() => {}); // a claim that isn't released lapses by itself
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
  const [stage, setStage] = useState<"details" | "camera" | "capture" | "review" | "done">("details");
  const [source, setSource] = useState<Source | null>(null);
  const [stepIndex, setStepIndex] = useState(0);
  const [captures, setCaptures] = useState<Captures>({});
  const steps = useQuery({
    queryKey: ["enrollment", "steps"],
    queryFn: () => api.get("/api/enrollment/steps"),
    staleTime: Infinity,
  });
  const sources = useQuery({
    queryKey: ["enrollment", "sources"],
    queryFn: () => api.get("/api/enrollment/sources"),
  });
  const required = (steps.data ?? []).filter((s) => s.required);
  const preselected = params.get("person");
  // Ask "Which camera?" only when there is more than this PC's webcam (or while that isn't known yet).
  const allSources = sources.data?.sources ?? null;
  const choose = sources.isPending || (allSources ?? []).some((s) => s.kind === "camera");
  const offset = choose ? 2 : 1;

  const labels = ["Details", ...(choose ? ["Camera"] : []), ...required.map((s) => s.title), "Review and save"];
  const current =
    stage === "details" ? 0 : stage === "camera" ? 1 : stage === "capture" ? offset + stepIndex : labels.length - 1;
  const capture = {
    person: person!,
    step: required[stepIndex]!,
    position: [stepIndex + 1, required.length] as [number, number],
    captured: captures,
    checkEveryMs,
    onCapture: (dataUrl: string) => {
      const next = { ...captures, [required[stepIndex]!.key]: dataUrl };
      setCaptures(next);
      const missing = required.findIndex((s) => !next[s.key]);
      if (missing === -1) setStage("review");
      else setStepIndex(missing);
    },
    onBack: () => (stepIndex > 0 ? setStepIndex(stepIndex - 1) : setStage(choose ? "camera" : "details")),
  };

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
              if (choose) setStage("camera");
              else {
                setSource({ kind: "webcam" });
                setStage("capture");
              }
            }}
          />
        ) : stage === "camera" ? (
          <CameraChoice
            sources={allSources}
            last={sources.data?.last ?? null}
            chosen={source}
            onBack={() => setStage("details")}
            onNext={(chosen) => {
              if (keyOf(chosen) !== keyOf(source)) setCaptures({}); // photos from one camera only
              setSource(chosen);
              setStepIndex(0);
              setStage("capture");
            }}
          />
        ) : stage === "capture" && person && required[stepIndex] && source ? (
          source.kind === "camera" ? (
            <IpCameraCapture key={source.id} cameraId={source.id} name={source.name} {...capture} />
          ) : (
            <WebcamCapture camera={camera} {...capture} />
          )
        ) : stage === "review" && person ? (
          <ReviewStep
            person={person}
            steps={required}
            captures={captures}
            source={source}
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
    defaultValues: { employee_code: "", name: "", department: "", shift: "" },
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

// --- Steps 3-5: guided capture ------------------------------------------------------------------------

/** Where the photos come from: this PC's webcam (in the browser) or an IP camera (read by the server). */
export type Source = { kind: "webcam" } | { kind: "camera"; id: number; name: string };

type Check = FrameCheck & { connected?: boolean; width?: number | null; height?: number | null; feed?: string };

/** A capture source as the steps use it; check and capture are stable. check() gives null when there's no frame. */
type Feed = {
  preview: React.ReactNode;
  mirrored: boolean;
  started: boolean;
  error: string | null;
  note: string | null;
  check: (step: string) => Promise<Check | null>;
  capture: (step: string) => Promise<string>;
};

type CaptureProps = {
  person: Person;
  step: StepInfo;
  position: [number, number];
  captured: Captures;
  checkEveryMs: number;
  onCapture: (dataUrl: string) => void;
  onBack: () => void;
};

function WebcamCapture({ camera, ...props }: CaptureProps & { camera?: CameraAdapter }) {
  return <CaptureView feed={useWebcamFeed(camera)} {...props} />;
}

function IpCameraCapture({ cameraId, name, ...props }: CaptureProps & { cameraId: number; name: string }) {
  return <CaptureView feed={useIpCameraFeed(cameraId, name)} {...props} />;
}

/** This PC's webcam: frames are grabbed in the browser and sent to /api/enrollment/check. */
function useWebcamFeed(camera?: CameraAdapter): Feed {
  const video = useRef<HTMLVideoElement>(null);
  const adapter = useRef<CameraAdapter>(camera ?? browserCamera());
  const [error, setError] = useState<string | null>(null);
  const [started, setStarted] = useState(false);

  // The webcam is claimed through the API before it is opened, so no webcam camera worker can start while the
  // wizard has it (and the wizard can't take it from a running worker). The claim is renewed while the capture
  // steps are open and released when they close (review, back, another page, the tab closing).
  useEffect(() => {
    const cam = adapter.current;
    let cancelled = false;
    let claimId: string | null = null;
    let renew: ReturnType<typeof setInterval> | undefined;
    const release = () => {
      clearInterval(renew);
      if (claimId) releaseWebcam(claimId);
      claimId = null;
    };
    const fail = (e: unknown) => {
      if (cancelled) return;
      cam.stop();
      release();
      setStarted(false);
      setError(e instanceof Error ? e.message : String(e));
    };
    void (async () => {
      try {
        const claim = await api.post("/api/enrollment/webcam", { body: {} });
        if (cancelled) return releaseWebcam(claim.claim_id);
        claimId = claim.claim_id;
        renew = setInterval(() => {
          api
            .post("/api/enrollment/webcam", { body: { claim_id: claimId } })
            .then((r) => {
              claimId = r.claim_id;
            })
            .catch(fail); // the claim lapsed and a webcam worker started meanwhile: give the webcam back
        }, claim.renew_seconds * 1000);
        if (!video.current) return;
        await cam.start(video.current);
        if (!cancelled) setStarted(true);
      } catch (e) {
        fail(e);
      }
    })();
    window.addEventListener("pagehide", release);
    return () => {
      cancelled = true;
      window.removeEventListener("pagehide", release);
      cam.stop();
      release();
    };
  }, []);

  const check = useCallback(async (step: string): Promise<Check | null> => {
    if (!video.current) return null;
    const blob = await adapter.current.grab(video.current, 0.8);
    return blob ? postImage("/api/enrollment/check", blob, { step }) : null;
  }, []);
  const capture = useCallback(async () => {
    const blob = video.current ? await adapter.current.grab(video.current, 0.92) : null;
    if (!blob) throw new Error("No picture from the camera yet.");
    return toDataUrl(blob);
  }, []);
  return {
    preview: (
      <video
        ref={video}
        muted
        playsInline
        aria-label="Camera preview"
        className="aspect-video w-full -scale-x-100 object-contain"
      />
    ),
    mirrored: true,
    started,
    error,
    note: null,
    check,
    capture,
  };
}

/** Close an IP camera session. keepalive: it still goes out while the tab is closing. */
function releaseSession(sessionId: string): void {
  void fetch(resourceUrl("/api/enrollment/sessions/{session_id}", { session_id: sessionId }), {
    method: "DELETE",
    credentials: "include",
    keepalive: true,
  }).catch(() => {}); // a session that isn't closed lapses by itself
}

/**
 * An IP camera: the server holds it for this wizard (sharing its running worker's frames, or opening the main
 * stream itself), serves a live preview and runs the checks and the capture on the full-resolution window. The
 * session is renewed while the capture steps are open and closed when they close.
 */
function useIpCameraFeed(cameraId: number, name: string): Feed {
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [info, setInfo] = useState<{ feed: string; size: string | null } | null>(null);
  const current = useRef<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    let renew: ReturnType<typeof setInterval> | undefined;
    const release = () => {
      clearInterval(renew);
      if (current.current) releaseSession(current.current);
      current.current = null;
    };
    const fail = (e: unknown) => {
      if (cancelled) return;
      release();
      setSessionId(null);
      setError(e instanceof Error ? e.message : String(e));
    };
    void (async () => {
      try {
        const session = await api.post("/api/enrollment/sessions", { body: { camera_id: cameraId } });
        if (cancelled) return releaseSession(session.session_id);
        current.current = session.session_id;
        setSessionId(session.session_id);
        renew = setInterval(() => {
          api
            .post("/api/enrollment/sessions", { body: { camera_id: cameraId, session_id: current.current } })
            .then((r) => {
              current.current = r.session_id; // a new one if the old one lapsed
              setSessionId(r.session_id);
            })
            .catch(fail);
        }, session.renew_seconds * 1000);
      } catch (e) {
        fail(e);
      }
    })();
    window.addEventListener("pagehide", release);
    return () => {
      cancelled = true;
      window.removeEventListener("pagehide", release);
      release();
    };
  }, [cameraId]);

  const check = useCallback(async (step: string): Promise<Check | null> => {
    const id = current.current;
    if (!id) return null;
    const r = await api.post("/api/enrollment/sessions/{session_id}/check", {
      path: { session_id: id },
      query: { step },
    });
    const size = r.width && r.height ? `${r.width}×${r.height}` : null;
    setInfo((old) => (old?.feed === r.feed && old.size === size ? old : { feed: r.feed, size }));
    return r;
  }, []);
  const capture = useCallback(async (step: string) => {
    const id = current.current;
    if (!id) throw new Error("The camera isn't open.");
    const r = await api.post("/api/enrollment/sessions/{session_id}/capture", {
      path: { session_id: id },
      query: { step },
    });
    return r.image;
  }, []);
  const note = info
    ? (info.feed === "worker"
        ? `Shared with the running ${name} camera (attendance goes on)`
        : `Read from the ${name} camera's main stream`) + (info.size ? ` · photo ${info.size}` : "")
    : null;
  return {
    preview: sessionId ? (
      <img
        src={resourceUrl("/api/enrollment/sessions/{session_id}/stream", { session_id: sessionId })}
        alt="Camera preview"
        className="block w-full"
      />
    ) : (
      <div className="aspect-video w-full" aria-label="Camera preview" />
    ),
    mirrored: false,
    started: sessionId !== null,
    error,
    note,
    check,
    capture,
  };
}

function CaptureView({
  feed,
  person,
  step,
  position,
  captured,
  checkEveryMs,
  onCapture,
  onBack,
}: CaptureProps & { feed: Feed }) {
  // The latest answer, kept with the step it was for (a new step starts without one).
  const [latest, setLatest] = useState<(Check & { step: string }) | null>(null);
  const check = latest?.step === step.key ? latest : null;
  const [checkError, setCheckError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const inFlight = useRef(false);
  const { started, check: runCheck } = feed;

  // A check every `checkEveryMs`, never two at once.
  useEffect(() => {
    if (!started) return;
    const timer = setInterval(async () => {
      if (inFlight.current) return;
      inFlight.current = true;
      try {
        const result = await runCheck(step.key);
        if (result) {
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
  }, [started, runCheck, step.key, checkEveryMs]);

  const ok = !!check?.ok;

  const capture = async () => {
    setBusy(true);
    try {
      onCapture(await feed.capture(step.key));
    } catch (e) {
      setCheckError(e instanceof ApiError || e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  // Boxes come from the raw frame; a mirrored preview (the webcam) mirrors x too.
  const place = (b: number[] | null | undefined) =>
    b
      ? {
          left: `${(feed.mirrored ? 1 - b[2]! : b[0]!) * 100}%`,
          top: `${b[1]! * 100}%`,
          width: `${(b[2]! - b[0]!) * 100}%`,
          height: `${(b[3]! - b[1]!) * 100}%`,
        }
      : null;
  const guide = place(check?.guide);
  const face = place(check?.box);

  return (
    <div className="grid gap-4 lg:grid-cols-3">
      <Card className="overflow-hidden lg:col-span-2">
        <div className="relative bg-black">
          {feed.preview}
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
          {feed.error && (
            <div
              role="alert"
              className="absolute inset-0 flex items-center justify-center p-4 text-center text-sm text-white"
            >
              {feed.error}
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
            {feed.error
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
          {feed.note && <p className="text-xs text-muted-foreground">{feed.note}</p>}
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
                  <img
                    src={url}
                    alt={`${key} photo`}
                    className={cn("h-12 w-16 rounded object-cover", feed.mirrored && "-scale-x-100")}
                  />
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

// --- Step 2 (when there are IP cameras): which camera ------------------------------------------------------

function sourceOf(key: string, sources: EnrollmentSource[]): Source {
  const s = sources.find((x) => x.key === key);
  return s?.kind === "camera" && s.camera_id != null
    ? { kind: "camera", id: s.camera_id, name: s.label }
    : { kind: "webcam" };
}

function keyOf(source: Source | null): string | null {
  return source === null ? null : source.kind === "webcam" ? "webcam" : `camera:${source.id}`;
}

function CameraChoice({
  sources,
  last,
  chosen,
  onNext,
  onBack,
}: {
  sources: EnrollmentSource[] | null;
  last: string | null;
  chosen: Source | null;
  onNext: (source: Source) => void;
  onBack: () => void;
}) {
  const [key, setKey] = useState<string>(keyOf(chosen) ?? last ?? "webcam");
  const list = sources ?? [];
  return (
    <Card>
      <CardContent className="flex flex-col gap-4">
        <div>
          <h2 className="text-base font-semibold" id="which-camera">
            Which camera?
          </h2>
          <p className="text-sm text-muted-foreground">
            An IP camera is read by GateSense at its full resolution; a camera that records attendance keeps doing so.
          </p>
        </div>
        {sources === null ? (
          <p className="text-sm text-muted-foreground">Loading the cameras…</p>
        ) : (
          <div role="radiogroup" aria-labelledby="which-camera" className="flex max-w-lg flex-col gap-2">
            {list.map((s) => (
              <label
                key={s.key}
                className={cn(
                  "flex cursor-pointer items-center gap-3 rounded-md border p-3 text-sm",
                  key === s.key && "border-primary bg-accent",
                )}
              >
                <input
                  type="radio"
                  name="enroll-source"
                  value={s.key}
                  checked={key === s.key}
                  onChange={() => setKey(s.key)}
                />
                {s.kind === "webcam" ? <Webcam className="size-4" /> : <Cctv className="size-4" />}
                <span className="flex flex-col">
                  <span className="font-medium">{s.label}</span>
                  <span className="text-xs text-muted-foreground">
                    {s.kind === "webcam"
                      ? "The browser's camera on this computer"
                      : [
                          s.direction === "OUT" ? "exit camera" : "entrance camera",
                          s.site,
                          s.running ? "recording attendance: its frames are shared" : null,
                        ]
                          .filter(Boolean)
                          .join(" · ")}
                  </span>
                </span>
              </label>
            ))}
          </div>
        )}
        <div className="flex gap-2">
          <Button variant="outline" onClick={onBack}>
            Back
          </Button>
          <Button disabled={sources === null} onClick={() => onNext(sourceOf(key, list))}>
            Next: photos
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}

// --- Last step: review and save ------------------------------------------------------------------------

function ReviewStep({
  person,
  steps,
  captures,
  source,
  onRetake,
  onSaved,
}: {
  person: Person;
  steps: StepInfo[];
  captures: Captures;
  source: Source | null;
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
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const save = async () => {
    setSaving(true);
    setError(null);
    try {
      await api.post("/api/people/{person_id}/enroll", {
        path: { person_id: person.id },
        body: { photos, source: source?.kind === "camera" ? source.name : "webcam" },
      });
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
                    className={cn(
                      "aspect-video w-full rounded object-cover",
                      source?.kind !== "camera" && "-scale-x-100",
                    )}
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
        {error && (
          <p role="alert" className="text-sm text-destructive">
            {error}
          </p>
        )}
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
