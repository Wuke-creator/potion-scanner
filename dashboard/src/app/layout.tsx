import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Potion Ops",
  description: "Operational dashboard for Potion Scanner",
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en" className="dark">
      <body className="min-h-screen">{children}</body>
    </html>
  );
}
