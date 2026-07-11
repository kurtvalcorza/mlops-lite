// 023 US3 (T505, FR-291): the CI-safe lint setup. `next lint` is deprecated in Next 15 (and
// removed in 16) and PROMPTS interactively when unconfigured — unusable as a required gate. This
// is the Next-recommended ESLint-CLI flat config (the next-lint-to-eslint-cli migration shape):
// the same next/core-web-vitals + next/typescript rules, run as plain `eslint` (package.json).
import { dirname } from "path";
import { fileURLToPath } from "url";
import { FlatCompat } from "@eslint/eslintrc";

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

const compat = new FlatCompat({ baseDirectory: __dirname });

const eslintConfig = [
  ...compat.extends("next/core-web-vitals", "next/typescript"),
  { ignores: [".next/**", "node_modules/**", "next-env.d.ts"] },
];

export default eslintConfig;
