import { redirect } from 'next/navigation';

// 027 (FR-363/364): the root lands on Overview, which answers health / what is running / what needs
// attention / what next. 021 landed on `/serving` — the loop's live heart — which was right when
// the IA *was* the loop and is wrong now that there are ten areas and no cycle to have a heart of.
export default function Home() {
  redirect('/overview');
}
