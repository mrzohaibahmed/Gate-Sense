import { zodResolver } from "@hookform/resolvers/zod";
import { useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useForm } from "react-hook-form";
import { Navigate, useNavigate, useSearchParams } from "react-router";
import { z } from "zod";

import { ApiError, api } from "@/api/client";
import { meKey, useMe } from "@/auth/auth";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Field, Input } from "@/components/ui/input";

const schema = z.object({
  username: z.string().trim().min(1, "Enter your user name"),
  password: z.string().min(1, "Enter your password"),
});
type Values = z.infer<typeof schema>;

/** Only same-site paths, never "//evil.example" or a full URL. */
export function safeNext(next: string | null): string {
  return next && next.startsWith("/") && !next.startsWith("//") && !next.startsWith("/login") ? next : "/";
}

function Centered({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex min-h-screen items-center justify-center bg-background p-4">
      <div className="flex w-full max-w-sm flex-col gap-4">
        <div className="flex items-center gap-2">
          <div className="flex size-8 items-center justify-center rounded-md bg-primary text-sm font-bold text-primary-foreground">
            GS
          </div>
          <span className="text-lg font-semibold">GateSense</span>
        </div>
        {children}
      </div>
    </div>
  );
}

export function LoginPage() {
  const { data: me } = useMe();
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [error, setError] = useState<string | null>(null);
  const form = useForm<Values>({ resolver: zodResolver(schema), defaultValues: { username: "", password: "" } });
  const next = safeNext(params.get("next"));

  if (me) return <Navigate to={me.must_change_password ? "/change-password" : next} replace />;

  const onSubmit = form.handleSubmit(async (values) => {
    setError(null);
    try {
      const user = await api.post("/api/auth/login", { body: values });
      queryClient.setQueryData(meKey, user);
      navigate(user.must_change_password ? "/change-password" : next, { replace: true });
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Can't reach the GateSense API. Is it running?");
    }
  });

  return (
    <Centered>
      <Card>
        <CardContent>
          <form onSubmit={onSubmit} className="flex flex-col gap-4" noValidate>
            <h1 className="text-base font-semibold">Log in</h1>
            <Field label="User name" htmlFor="username" error={form.formState.errors.username?.message}>
              <Input id="username" autoComplete="username" autoFocus {...form.register("username")} />
            </Field>
            <Field label="Password" htmlFor="password" error={form.formState.errors.password?.message}>
              <Input id="password" type="password" autoComplete="current-password" {...form.register("password")} />
            </Field>
            {error && (
              <p role="alert" className="rounded-md bg-destructive/10 px-2 py-1.5 text-sm text-destructive">
                {error}
              </p>
            )}
            <Button type="submit" disabled={form.formState.isSubmitting}>
              {form.formState.isSubmitting ? "Logging in…" : "Log in"}
            </Button>
          </form>
        </CardContent>
      </Card>
    </Centered>
  );
}

const MIN = 10;
const passwordSchema = z
  .object({
    current_password: z.string().min(1, "Enter your current password"),
    new_password: z.string().min(MIN, `At least ${MIN} characters`),
    confirm: z.string(),
  })
  .refine((v) => v.new_password === v.confirm, { path: ["confirm"], message: "The two passwords differ" })
  .refine((v) => v.new_password !== v.current_password, {
    path: ["new_password"],
    message: "Choose a different password",
  });
type PasswordValues = z.infer<typeof passwordSchema>;

/** Required after the first login and after an admin reset; also reachable from the user menu. */
export function ChangePasswordPage() {
  const { data: me } = useMe();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [error, setError] = useState<string | null>(null);
  const form = useForm<PasswordValues>({
    resolver: zodResolver(passwordSchema),
    defaultValues: { current_password: "", new_password: "", confirm: "" },
  });
  const forced = !!me?.must_change_password;

  const onSubmit = form.handleSubmit(async ({ current_password, new_password }) => {
    setError(null);
    try {
      await api.post("/api/auth/password", { body: { current_password, new_password } });
      await queryClient.invalidateQueries({ queryKey: meKey });
      navigate("/", { replace: true });
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Couldn't change the password.");
    }
  });

  return (
    <Centered>
      <Card>
        <CardContent>
          <form onSubmit={onSubmit} className="flex flex-col gap-4" noValidate>
            <div>
              <h1 className="text-base font-semibold">Change your password</h1>
              {forced && (
                <p className="mt-1 text-sm text-muted-foreground">
                  Your password was set by an administrator. Choose your own before you continue.
                </p>
              )}
            </div>
            <Field label="Current password" htmlFor="current" error={form.formState.errors.current_password?.message}>
              <Input
                id="current"
                type="password"
                autoComplete="current-password"
                {...form.register("current_password")}
              />
            </Field>
            <Field
              label="New password"
              htmlFor="new"
              error={form.formState.errors.new_password?.message}
              hint={`At least ${MIN} characters.`}
            >
              <Input id="new" type="password" autoComplete="new-password" {...form.register("new_password")} />
            </Field>
            <Field label="New password again" htmlFor="confirm" error={form.formState.errors.confirm?.message}>
              <Input id="confirm" type="password" autoComplete="new-password" {...form.register("confirm")} />
            </Field>
            {error && (
              <p role="alert" className="rounded-md bg-destructive/10 px-2 py-1.5 text-sm text-destructive">
                {error}
              </p>
            )}
            <div className="flex gap-2">
              <Button type="submit" disabled={form.formState.isSubmitting}>
                Change password
              </Button>
              {!forced && (
                <Button variant="outline" onClick={() => navigate(-1)}>
                  Cancel
                </Button>
              )}
            </div>
          </form>
        </CardContent>
      </Card>
    </Centered>
  );
}
