import type { Metadata } from "next";
import { Workspace } from "@/components/workspace";
import "./globals.css";
export const metadata: Metadata = {
  title: "Market Intelligence",
  description: "Evidence-led cross-asset market intelligence",
};
export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en">
      <body>
        <Workspace>{children}</Workspace>
      </body>
    </html>
  );
}
