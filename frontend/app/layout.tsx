import "./globals.css";
import type { Metadata } from "next";
import Link from "next/link";

export const metadata: Metadata = {
  title: "Meridian Lending",
  description: "Loan origination + servicing portal",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en">
      <body>
        <nav>
          <Link href="/">Meridian Lending</Link>
          <Link href="/apply">Apply</Link>
          <Link href="/servicing">Servicing</Link>
        </nav>
        {children}
      </body>
    </html>
  );
}
