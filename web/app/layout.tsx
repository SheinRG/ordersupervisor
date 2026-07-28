import type { Metadata } from "next";
import Link from "next/link";
import "./globals.css";

// Deliberately no next/font/google: it fetches at build time, which is an
// avoidable network dependency for a local demo.

export const metadata: Metadata = {
  title: "Order Supervisor",
  description: "Long-running AI supervision of a single order, on Temporal",
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en" className="h-full antialiased">
      <body className="min-h-full bg-zinc-50 text-zinc-900">
        <header className="border-b border-zinc-200 bg-white">
          <div className="mx-auto flex max-w-7xl items-center gap-6 px-6 py-3">
            <Link href="/" className="text-sm font-semibold tracking-tight">
              Order Supervisor
            </Link>
            <nav className="flex gap-4 text-sm text-zinc-600">
              <Link href="/" className="hover:text-zinc-900">
                Runs
              </Link>
              <Link href="/supervisors" className="hover:text-zinc-900">
                Supervisors
              </Link>
              <a
                href="http://localhost:8233"
                target="_blank"
                rel="noreferrer"
                className="hover:text-zinc-900"
              >
                Temporal UI &#8599;
              </a>
            </nav>
          </div>
        </header>
        <main className="mx-auto max-w-7xl px-6 py-6">{children}</main>
      </body>
    </html>
  );
}
