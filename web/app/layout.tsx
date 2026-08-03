import type { Metadata, Viewport } from "next";
import { IBM_Plex_Mono, IBM_Plex_Sans } from "next/font/google";
import "./globals.css";

const sans = IBM_Plex_Sans({
  subsets: ["latin"],
  weight: ["400", "500", "600"],
  variable: "--font-plex-sans",
});
const mono = IBM_Plex_Mono({
  subsets: ["latin"],
  weight: ["400", "500"],
  variable: "--font-plex-mono",
});

export const metadata: Metadata = {
  title: "Auto Lead",
  description: "Massachusetts parcels where the land outvalues the structure",
};

// Without this a phone lays the page out at ~774px and scales it down, which
// makes the detail panel unreadable in front of the house.
export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  themeColor: "#121614",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className={`${sans.variable} ${mono.variable}`}>
      <body className="h-full overflow-hidden">{children}</body>
    </html>
  );
}
