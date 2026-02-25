"use client";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { LayoutDashboard, Radio, BookOpen, ClipboardCheck, Zap, Bot } from "lucide-react";
import { cn } from "@/lib/utils";

const NAV = [
  { href: "/dashboard",     icon: LayoutDashboard, label: "Dashboard" },
  { href: "/scraper",       icon: Radio,           label: "Scraper" },
  { href: "/glossary",      icon: BookOpen,        label: "Glossary" },
  { href: "/manual-review", icon: ClipboardCheck,  label: "Manual Review" },
  { href: "/automation",    icon: Bot,             label: "Automation" },
];

export function Sidebar() {
  const path = usePathname();

  return (
    <aside className="fixed inset-y-0 left-0 z-50 flex w-56 flex-col border-r border-border/60 bg-card">
      {/* Logo */}
      <div className="flex h-14 items-center gap-2.5 border-b border-border/60 px-4">
        <div className="flex h-7 w-7 items-center justify-center rounded-lg bg-gradient-to-br from-purple-500 to-pink-500 shadow-md">
          <Zap className="h-3.5 w-3.5 text-white" />
        </div>
        <div>
          <p className="text-sm font-heading font-bold gradient-text leading-none">TenAsia IH</p>
          <p className="mt-0.5 text-[9px] uppercase tracking-widest text-muted-foreground">Intelligence Hub</p>
        </div>
      </div>

      {/* Nav */}
      <nav className="flex flex-col gap-1 p-3 pt-4">
        <p className="mb-1 px-3 text-[9px] font-semibold uppercase tracking-widest text-muted-foreground/50">
          Navigation
        </p>
        {NAV.map(({ href, icon: Icon, label }) => {
          const active = path === href || path.startsWith(href + "/");
          return (
            <Link key={href} href={href} className={cn("nav-item", active && "active")}>
              <Icon className="h-4 w-4 shrink-0" />
              {label}
              {active && <div className="ml-auto h-1 w-1 rounded-full bg-primary" />}
            </Link>
          );
        })}
      </nav>

      {/* Footer */}
      <div className="mt-auto border-t border-border/60 p-4">
        <div className="flex items-center gap-2 text-[10px] text-muted-foreground">
          <span className="inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-emerald-400" />
          FastAPI :8000
        </div>
      </div>
    </aside>
  );
}
