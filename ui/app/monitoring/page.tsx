import { redirect } from 'next/navigation';

// 027 T699 (FR-364 / SC-186): `/monitoring` is a retired 021 path. Every retired path RESOLVES — a
// console that renames its areas and leaves the old URLs 404ing breaks every bookmark, every link
// in a runbook, and every reference in an incident write-up. A permanent redirect says the move is
// the new truth rather than a temporary detour.
export default function RetiredPath() {
  redirect('/observability');
}
