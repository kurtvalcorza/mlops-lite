import type { Metadata } from 'next';
import { JetBrains_Mono } from 'next/font/google';
import './globals.css';
import { Nav } from '@/components/Nav';

// Self-hosted at build time by next/font (Principle V — OSS, no paid Berkeley Mono; Principle I —
// no runtime external request). Exposed to Tailwind/CSS as --font-mono.
const jetbrains = JetBrains_Mono({
  subsets: ['latin'],
  weight: ['400', '500', '700'],
  variable: '--font-mono',
  display: 'swap',
});

export const metadata: Metadata = {
  title: 'MLOPS-LITE — operator console',
  description: 'Local-first MLOps control plane. Native WSL, 127.0.0.1 only.',
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className={jetbrains.variable}>
      <body className="min-h-screen font-mono text-body-md">
        <Nav />
        <main className="mx-auto w-full max-w-[1100px] px-6 py-8">{children}</main>
      </body>
    </html>
  );
}
