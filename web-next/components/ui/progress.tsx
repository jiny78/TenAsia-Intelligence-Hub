"use client";
import * as React from "react";
import { cn } from "@/lib/utils";

interface ProgressProps extends React.HTMLAttributes<HTMLDivElement> {
  value?: number;          // 0-100
  max?: number;
  label?: string;
  showValue?: boolean;
  variant?: "default" | "gradient" | "success";
  size?: "sm" | "md" | "lg";
}

const sizeH = { sm: "h-1.5", md: "h-2.5", lg: "h-4" };

const barVariant = {
  default:  "bg-primary",
  gradient: "bg-gradient-to-r from-violet-500 via-purple-500 to-pink-500",
  success:  "bg-gradient-to-r from-emerald-400 to-teal-400",
};

export function Progress({
  value = 0,
  max = 100,
  label,
  showValue = false,
  variant = "gradient",
  size = "md",
  className,
  ...props
}: ProgressProps) {
  const pct = Math.min(100, Math.max(0, (value / max) * 100));

  return (
    <div className={cn("w-full space-y-1.5", className)} {...props}>
      {(label || showValue) && (
        <div className="flex items-center justify-between text-xs">
          {label && <span className="text-muted-foreground">{label}</span>}
          {showValue && <span className="font-medium tabular-nums">{pct.toFixed(0)}%</span>}
        </div>
      )}
      <div className={cn("w-full overflow-hidden rounded-full bg-muted", sizeH[size])}>
        <div
          role="progressbar"
          aria-valuenow={pct}
          aria-valuemin={0}
          aria-valuemax={100}
          className={cn(
            "h-full rounded-full transition-all duration-500 ease-out",
            barVariant[variant],
            pct === 100 && variant === "gradient" && "animate-pulse"
          )}
          style={{ width: `${pct}%` }}
        />
      </div>
    </div>
  );
}
