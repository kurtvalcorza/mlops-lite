// 027 T697 — the ten console areas.
//
// The IA is **areas of concern**, not a lifecycle loop. 021's nav was the loop itself — six ordered
// stages with directional connectors — which reads well when the platform *is* a loop and stops
// reading at all once there are ten concerns that do not form a cycle. Extending the loop in place
// was offered and declined for exactly that reason: a ten-item "loop" is no longer a loop.
//
// **No area is named after a backing service** (FR-365). An operator should not have to know which
// process answers a question in order to find where to ask it — "Runtime", not "host agent";
// "Deployments", not "serving supervisor"; "Observability", not "Prometheus".

import type { NavArea } from './platform-types';

export const AREAS: NavArea[] = [
  {
    slug: 'overview',
    label: 'Overview',
    description: 'Health, what is running, what needs attention, what to do next',
  },
  {
    slug: 'models',
    label: 'Models',
    description: 'One catalog across the registry, artifacts, deployments, and evaluations',
  },
  {
    slug: 'training',
    label: 'Training',
    description: 'Runs, studies, and their live progress',
  },
  {
    slug: 'evaluations',
    label: 'Evaluations',
    description: 'Quality gates, comparisons, and drift',
    children: [{ slug: 'drift', label: 'Drift' }],
  },
  {
    slug: 'deployments',
    label: 'Deployments',
    description: 'What is serving, and what is promoted to serve',
  },
  {
    slug: 'inference',
    label: 'Inference',
    description: 'Send a request; read the record it produced',
  },
  {
    slug: 'datasets',
    label: 'Datasets',
    description: 'Versioned data and the artifacts derived from it',
  },
  {
    slug: 'runtime',
    label: 'Runtime',
    description: 'Devices, engine processes, admission decisions, and the journal',
  },
  {
    slug: 'observability',
    label: 'Observability',
    description: 'Platform health, metrics, and monitoring',
    children: [{ slug: 'health', label: 'Health' }],
  },
  {
    slug: 'administration',
    label: 'Administration',
    description: 'Tenants, quotas, and the usage ledger',
  },
];

/**
 * 021 → 027 redirect map (FR-364). Every retired path resolves to a successor area; none 404s.
 *
 * `/retraining` → `/evaluations/drift` is the one judgement call: the retraining stage's content was
 * policies, the cycle board, and suggestions, which split across Evaluations and a later increment's
 * suggestion review. Drift is the closest live destination in 027.
 */
export const REDIRECTS: Record<string, string> = {
  '/serving': '/deployments',
  '/data': '/datasets',
  '/monitor': '/observability',
  '/monitoring': '/observability',
  '/retraining': '/evaluations/drift',
  '/infer': '/inference',
  '/runs': '/training',
  '/health': '/observability/health',
};

export function areaFor(pathname: string): NavArea | undefined {
  const top = '/' + (pathname.split('/')[1] ?? '');
  return AREAS.find((a) => '/' + a.slug === top);
}
