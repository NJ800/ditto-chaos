// Side drawer in the structure of shadcn/ui's Sheet (a Radix Dialog), written against this
// project's tokens. Used for the navigation on small screens. Radix supplies the focus
// trap, Escape to close, scroll lock and the dialog semantics.
import * as Dialog from "@radix-ui/react-dialog";
import { X } from "lucide-react";
import type { ReactNode } from "react";
import { cn } from "../lib/utils";

export const Sheet = Dialog.Root;
export const SheetTrigger = Dialog.Trigger;
export const SheetClose = Dialog.Close;

export function SheetContent({ title, children, className }: { title: string; children: ReactNode; className?: string }) {
  return (
    <Dialog.Portal>
      <Dialog.Overlay className="fixed inset-0 z-40 bg-ink/40" />
      <Dialog.Content aria-describedby={undefined} className={cn("fixed inset-y-0 left-0 z-50 flex w-64 max-w-[85vw] flex-col border-r border-line bg-surface shadow-lg", className)}>
        <div className="flex h-12 items-center justify-between border-b border-line px-4">
          <Dialog.Title className="text-base font-bold tracking-tight text-ink">{title}</Dialog.Title>
          <Dialog.Close className="rounded-md p-1.5 text-dim hover:bg-raised hover:text-ink" aria-label="Close navigation">
            <X className="h-4 w-4" aria-hidden="true" />
          </Dialog.Close>
        </div>
        {children}
      </Dialog.Content>
    </Dialog.Portal>
  );
}
