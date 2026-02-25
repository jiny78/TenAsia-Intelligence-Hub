"use client";
import { ThemeProvider } from "next-themes";
import { SWRConfig } from "swr";

export function Providers({ children }: { children: React.ReactNode }) {
  return (
    <ThemeProvider attribute="class" defaultTheme="dark" enableSystem={false} disableTransitionOnChange>
      <SWRConfig value={{ onError: (e) => console.error("[SWR]", e) }}>
        {children}
      </SWRConfig>
    </ThemeProvider>
  );
}
