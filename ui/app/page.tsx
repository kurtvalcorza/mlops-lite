import { redirect } from 'next/navigation';

// 021 (FR-212): land on the loop's live heart — serving.
export default function Home() {
  redirect('/serving');
}
