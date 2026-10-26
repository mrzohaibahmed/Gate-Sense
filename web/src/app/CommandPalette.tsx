import { useQuery } from "@tanstack/react-query";
import { Command } from "cmdk";
import { UserRound } from "lucide-react";
import { useEffect, useState } from "react";
import { useNavigate } from "react-router";

import { api } from "@/api/client";
import { can, canAny, useMe } from "@/auth/auth";
import { Dialog, DialogContent, DialogTitle } from "@/components/ui/dialog";

import { NAV } from "./nav";
import { useShiftsEnabled } from "./shiftMode";

/** Ctrl+K / ⌘K: jump to a page, or find a person by name or code. */
export function CommandPalette({ open, onOpenChange }: { open: boolean; onOpenChange: (open: boolean) => void }) {
  const { data: me } = useMe();
  const shifts = useShiftsEnabled();
  const navigate = useNavigate();
  const [search, setSearch] = useState("");
  const [debounced, setDebounced] = useState("");

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        onOpenChange(!open);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onOpenChange]);

  useEffect(() => {
    const t = setTimeout(() => setDebounced(search.trim()), 200);
    return () => clearTimeout(t);
  }, [search]);

  const people = useQuery({
    queryKey: ["people", "search", debounced],
    queryFn: () => api.get("/api/people", { query: { q: debounced, page_size: 8 } }),
    enabled: open && debounced.length > 0 && can(me, "people:read"),
  });

  const go = (to: string) => {
    onOpenChange(false);
    setSearch("");
    navigate(to);
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent hideClose className="max-w-xl gap-0 p-0">
        <DialogTitle className="sr-only">Search</DialogTitle>
        <Command shouldFilter={false} className="flex flex-col">
          <Command.Input
            value={search}
            onValueChange={setSearch}
            placeholder="Search people, or go to a page…"
            className="h-11 border-b bg-transparent px-4 text-sm outline-none"
          />
          <Command.List className="max-h-80 overflow-y-auto p-2">
            <Command.Empty className="py-6 text-center text-sm text-muted-foreground">No results.</Command.Empty>
            {(people.data?.items.length ?? 0) > 0 && (
              <Command.Group
                heading="People"
                className="text-xs text-muted-foreground [&_[cmdk-group-heading]]:px-2 [&_[cmdk-group-heading]]:py-1"
              >
                {people.data?.items.map((p) => (
                  <Command.Item
                    key={p.id}
                    value={`person-${p.id}`}
                    onSelect={() => go(`/people/${p.id}`)}
                    className="flex cursor-pointer items-center gap-2 rounded-md px-2 py-1.5 text-sm text-foreground data-[selected=true]:bg-muted"
                  >
                    <UserRound className="size-4 text-muted-foreground" />
                    <span>{p.name}</span>
                    <span className="ml-auto text-xs text-muted-foreground">{p.employee_code}</span>
                  </Command.Item>
                ))}
              </Command.Group>
            )}
            <Command.Group
              heading="Pages"
              className="text-xs text-muted-foreground [&_[cmdk-group-heading]]:px-2 [&_[cmdk-group-heading]]:py-1"
            >
              {NAV.filter((n) => canAny(me, n.permissions) && (shifts || !n.shifts))
                .filter((n) => !search || n.label.toLowerCase().includes(search.toLowerCase()))
                .map((n) => (
                  <Command.Item
                    key={n.to}
                    value={`page-${n.to}`}
                    onSelect={() => go(n.to)}
                    className="flex cursor-pointer items-center gap-2 rounded-md px-2 py-1.5 text-sm text-foreground data-[selected=true]:bg-muted"
                  >
                    <n.icon className="size-4 text-muted-foreground" />
                    {n.label}
                  </Command.Item>
                ))}
            </Command.Group>
          </Command.List>
        </Command>
      </DialogContent>
    </Dialog>
  );
}
