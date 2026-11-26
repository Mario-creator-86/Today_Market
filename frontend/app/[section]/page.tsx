import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { SectionView } from "@/components/pages/section-view";
import { SECTION_TITLES } from "@/components/pages/titles";

export async function generateMetadata({
  params,
}: PageProps<"/[section]">): Promise<Metadata> {
  const { section } = await params;
  const title = Object.hasOwn(SECTION_TITLES, section)
    ? SECTION_TITLES[section]
    : "Page not found";
  return { title: `${title} · Market Intelligence` };
}

export default async function Page({ params }: PageProps<"/[section]">) {
  const { section } = await params;
  // Unknown sections are a real 404 (app/not-found.tsx), not a page that answers 200.
  if (!Object.hasOwn(SECTION_TITLES, section)) notFound();
  return <SectionView section={section} />;
}
