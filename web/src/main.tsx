import "./index.css";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { createBrowserRouter, RouterProvider } from "react-router";
import { Toaster } from "sonner";

import { ApiError, setUnauthorizedHandler } from "@/api/client";
import { routes } from "@/app/routes";
import { applyTheme } from "@/app/theme";
import { meKey } from "@/auth/auth";

applyTheme();

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 15_000,
      // A refused or missing thing won't appear on a retry; a network blip might.
      retry: (count, error) => !(error instanceof ApiError && error.status < 500) && count < 2,
      refetchOnWindowFocus: true,
    },
  },
});

const router = createBrowserRouter(routes);

// Any 401 (the session expired, the account was deactivated): forget the user and go to the login page,
// coming back to this page afterwards.
setUnauthorizedHandler(() => {
  queryClient.setQueryData(meKey, null);
  const here = router.state.location;
  if (!here.pathname.startsWith("/login")) {
    void router.navigate(`/login?next=${encodeURIComponent(here.pathname + here.search)}`, { replace: true });
  }
});

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
      <Toaster position="bottom-right" richColors closeButton />
    </QueryClientProvider>
  </StrictMode>,
);
