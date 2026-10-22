import { Construction } from "lucide-react";

import { PageHeader } from "@/app/AppShell";
import { Card } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/misc";

/** A screen that isn't built yet: the shell, the menu and the permission check already work. */
export function PlaceholderPage({ title }: { title: string }) {
  return (
    <>
      <PageHeader title={title} />
      <div className="px-4 pb-4">
        <Card>
          <EmptyState icon={<Construction />} title={`${title} comes next`}>
            This screen is part of the next build step.
          </EmptyState>
        </Card>
      </div>
    </>
  );
}
