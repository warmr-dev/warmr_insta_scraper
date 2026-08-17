import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Warmr Admin",
  description: "Instagram stories monitoring and lead analytics",
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body className="bg-slate-950 text-slate-100 antialiased">{children}</body>
    </html>
  );
}
