import {
  type ColumnDef,
  flexRender,
  getCoreRowModel,
  type Row,
  useReactTable,
  type VisibilityState,
} from "@tanstack/react-table";
import { ArrowDown, ArrowUp, ChevronLeft, ChevronRight, Columns3, Inbox } from "lucide-react";
import { useState } from "react";

import { TableSkeleton } from "@/components/states";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuCheckboxItem,
  DropdownMenuContent,
  DropdownMenuLabel,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { EmptyState, ErrorState, Td, Th } from "@/components/ui/misc";
import { cn } from "@/lib/utils";

export type ColumnMeta = { label?: string; sortKey?: string; className?: string };

/** "-date" -> {key: "date", desc: true} */
export function parseSort(sort: string | undefined): { key: string; desc: boolean } | null {
  if (!sort) return null;
  return { key: sort.replace(/^[-+]/, ""), desc: sort.startsWith("-") };
}

/**
 * A server-paged table (TanStack Table): sticky header, sort by clicking a header (the server sorts), a column
 * picker, and loading / empty / error states. Rows scroll inside the card; on a phone the table scrolls sideways.
 */
export function DataTable<T>({
  columns,
  data,
  total,
  page,
  pageSize,
  onPage,
  sort,
  onSort,
  isLoading,
  error,
  retry,
  onRowClick,
  empty,
  initialHidden,
  toolbar,
  rowLabel,
  maxHeight = "calc(100vh - 220px)",
}: {
  columns: ColumnDef<T>[];
  data: T[] | undefined;
  total?: number;
  page?: number;
  pageSize?: number;
  onPage?: (page: number) => void;
  sort?: string;
  onSort?: (sort: string) => void;
  isLoading?: boolean;
  error?: unknown;
  retry?: () => void;
  onRowClick?: (row: T) => void;
  empty?: React.ReactNode;
  initialHidden?: string[];
  toolbar?: React.ReactNode;
  rowLabel?: (row: T) => string;
  maxHeight?: string;
}) {
  const [visibility, setVisibility] = useState<VisibilityState>(
    Object.fromEntries((initialHidden ?? []).map((id) => [id, false])),
  );
  const table = useReactTable({
    data: data ?? [],
    columns,
    state: { columnVisibility: visibility },
    onColumnVisibilityChange: setVisibility,
    getCoreRowModel: getCoreRowModel(),
    manualPagination: true,
    manualSorting: true,
  });
  const current = parseSort(sort);
  const pages = total !== undefined && pageSize ? Math.max(1, Math.ceil(total / pageSize)) : 1;

  const header = (id: string, meta: ColumnMeta | undefined, content: React.ReactNode) => {
    if (!meta?.sortKey || !onSort) return content;
    const active = current?.key === meta.sortKey;
    const next = active && !current?.desc ? `-${meta.sortKey}` : meta.sortKey;
    return (
      <button
        type="button"
        className="inline-flex items-center gap-1 hover:text-foreground"
        onClick={() => onSort(next)}
        aria-label={`Sort by ${meta.label ?? id}`}
      >
        {content}
        {active && (current?.desc ? <ArrowDown className="size-3" /> : <ArrowUp className="size-3" />)}
      </button>
    );
  };

  return (
    <div className="flex flex-col rounded-lg border bg-card">
      <div className="flex flex-wrap items-center gap-2 border-b p-2">
        {toolbar}
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button variant="outline" size="sm" className="ml-auto" aria-label="Columns">
              <Columns3 /> Columns
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end">
            <DropdownMenuLabel>Show columns</DropdownMenuLabel>
            {table
              .getAllLeafColumns()
              .filter((c) => c.getCanHide())
              .map((c) => (
                <DropdownMenuCheckboxItem
                  key={c.id}
                  checked={c.getIsVisible()}
                  onCheckedChange={(v) => c.toggleVisibility(!!v)}
                  onSelect={(e) => e.preventDefault()}
                >
                  {(c.columnDef.meta as ColumnMeta | undefined)?.label ?? c.id}
                </DropdownMenuCheckboxItem>
              ))}
          </DropdownMenuContent>
        </DropdownMenu>
      </div>
      {error ? (
        <ErrorState error={error} retry={retry} />
      ) : isLoading && !data ? (
        <TableSkeleton />
      ) : !data?.length ? (
        (empty ?? <EmptyState icon={<Inbox />} title="Nothing here" />)
      ) : (
        <div className="overflow-auto" style={{ maxHeight }}>
          <table className="w-full border-collapse text-sm">
            <thead>
              {table.getHeaderGroups().map((group) => (
                <tr key={group.id}>
                  {group.headers.map((h) => {
                    const meta = h.column.columnDef.meta as ColumnMeta | undefined;
                    return (
                      <Th key={h.id} className={meta?.className}>
                        {h.isPlaceholder
                          ? null
                          : header(h.column.id, meta, flexRender(h.column.columnDef.header, h.getContext()))}
                      </Th>
                    );
                  })}
                </tr>
              ))}
            </thead>
            <tbody>
              {table.getRowModel().rows.map((row: Row<T>) => (
                <tr
                  key={row.id}
                  onClick={onRowClick ? () => onRowClick(row.original) : undefined}
                  onKeyDown={
                    onRowClick
                      ? (e) => {
                          if (e.key === "Enter") onRowClick(row.original);
                        }
                      : undefined
                  }
                  tabIndex={onRowClick ? 0 : undefined}
                  aria-label={rowLabel?.(row.original)}
                  className={cn(onRowClick && "cursor-pointer hover:bg-muted/60 focus-visible:bg-muted")}
                >
                  {row.getVisibleCells().map((cell) => (
                    <Td key={cell.id} className={(cell.column.columnDef.meta as ColumnMeta | undefined)?.className}>
                      {flexRender(cell.column.columnDef.cell, cell.getContext())}
                    </Td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {onPage && total !== undefined && total > 0 && (
        <div className="flex items-center justify-end gap-2 border-t px-2 py-1.5 text-xs text-muted-foreground">
          <span>
            {total} row{total === 1 ? "" : "s"} · page {page} of {pages}
          </span>
          <Button
            variant="ghost"
            size="icon"
            disabled={(page ?? 1) <= 1}
            onClick={() => onPage((page ?? 1) - 1)}
            aria-label="Previous page"
          >
            <ChevronLeft />
          </Button>
          <Button
            variant="ghost"
            size="icon"
            disabled={(page ?? 1) >= pages}
            onClick={() => onPage((page ?? 1) + 1)}
            aria-label="Next page"
          >
            <ChevronRight />
          </Button>
        </div>
      )}
    </div>
  );
}
