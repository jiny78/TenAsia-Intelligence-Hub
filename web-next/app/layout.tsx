import type { Metadata } from "next";
import { Inter, Outfit, Noto_Sans_KR } from "next/font/google";
import { Providers } from "@/components/layout/providers";
import { Sidebar } from "@/components/layout/sidebar";
import { ThemeToggle } from "@/components/layout/theme-toggle";
import "./globals.css";

const inter   = Inter({ subsets: ["latin"], variable: "--font-inter" });
const outfit  = Outfit({ subsets: ["latin"], weight: ["400","500","600","700","800"], variable: "--font-outfit" });
const notoKr  = Noto_Sans_KR({ subsets: ["latin"], weight: ["300","400","500","700"], variable: "--font-noto-kr" });

export const metadata: Metadata = {
  title: { default: "TenAsia IH", template: "%s | TenAsia IH" },
  description: "K-Entertainment Intelligence Hub",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="ko" suppressHydrationWarning>
      <body className={`${inter.variable} ${outfit.variable} ${notoKr.variable} font-sans antialiased`}>
        <Providers>
          <div className="flex h-screen overflow-hidden">
            <Sidebar />
            <div className="ml-56 flex flex-1 flex-col overflow-hidden">
              {/* Top bar */}
              <header className="flex h-14 items-center justify-end border-b border-border/60 bg-background/80 px-6 backdrop-blur-md">
                <ThemeToggle />
              </header>
              <main className="flex-1 overflow-y-auto thin-scroll p-6">
                {children}
              </main>
            </div>
          </div>
        </Providers>
      </body>
    </html>
  );
}
