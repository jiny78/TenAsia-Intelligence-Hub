import { cn } from "@/lib/utils";
import type { ProcessStatus } from "@/lib/types";

const CFG: Record<
  ProcessStatus,
  { label: string; dot: string; text: string; bg: string; border: string }
> = {
  PROCESSED:     { label: "완료",      dot: "bg-emerald-400", text: "text-emerald-400", bg: "bg-emerald-400/10", border: "border-emerald-400/20" },
  SCRAPED:       { label: "수집",      dot: "bg-blue-400",    text: "text-blue-400",    bg: "bg-blue-400/10",    border: "border-blue-400/20"    },
  PENDING:       { label: "대기",      dot: "bg-zinc-400",    text: "text-zinc-400",    bg: "bg-zinc-400/10",    border: "border-zinc-400/20"    },
  MANUAL_REVIEW: { label: "검토 필요", dot: "bg-amber-400",   text: "text-amber-400",   bg: "bg-amber-400/10",   border: "border-amber-400/20"   },
  ERROR:         { label: "오류",      dot: "bg-red-400",     text: "text-red-400",     bg: "bg-red-400/10",     border: "border-red-400/20"     },
};

interface StatusBadgeProps {
  status: ProcessStatus | null | undefined;
  withDot?: boolean;
}

export function StatusBadge({ status, withDot = true }: StatusBadgeProps) {
  if (!status) return null;
  const c = CFG[status] ?? CFG.PENDING;
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide",
        c.text, c.bg, c.border
      )}
    >
      {withDot && <span className={cn("inline-block h-1.5 w-1.5 rounded-full", c.dot)} />}
      {c.label}
    </span>
  );
}
