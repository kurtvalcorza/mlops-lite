import { redirect } from 'next/navigation';

// 027 T699 (FR-364): `/health` was 021's off-loop health view; it now lives under Observability.
// The probe endpoints `/healthz` and `/readyz` are UNCHANGED — they are probes, not navigation,
// and moving them would break liveness checks that have nothing to do with the console's IA.
export default function RetiredPath() {
  redirect('/observability/health');
}
