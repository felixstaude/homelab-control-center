import js from '@eslint/js';
import jsxA11y from 'eslint-plugin-jsx-a11y';
import reactHooks from 'eslint-plugin-react-hooks';
import reactRefresh from 'eslint-plugin-react-refresh';
import globals from 'globals';
import tseslint from 'typescript-eslint';

/**
 * ESLint flat configuration.
 *
 * Uses typescript-eslint's type-aware rules, which catch classes of bug that
 * syntax-only linting cannot (unawaited promises, unsafe `any` flow). Those
 * rules need a TypeScript program, so they are scoped to `**` / `*.ts(x)`
 * only — applying them to plain `.js` files (including this one) makes ESLint
 * fail outright, because no tsconfig covers them.
 */
export default tseslint.config(
  { ignores: ['dist', 'node_modules', 'coverage', '**/*.tsbuildinfo'] },

  js.configs.recommended,

  // --- TypeScript sources: full type-aware linting ---
  {
    files: ['**/*.{ts,tsx}'],
    extends: [...tseslint.configs.strictTypeChecked, ...tseslint.configs.stylisticTypeChecked],
    languageOptions: {
      ecmaVersion: 2022,
      globals: { ...globals.browser, ...globals.es2022 },
      parserOptions: {
        projectService: true,
        tsconfigRootDir: import.meta.dirname,
      },
    },
    plugins: {
      'react-hooks': reactHooks,
      'react-refresh': reactRefresh,
      'jsx-a11y': jsxA11y,
    },
    rules: {
      ...reactHooks.configs.recommended.rules,
      ...jsxA11y.flatConfigs.recommended.rules,

      'react-refresh/only-export-components': ['warn', { allowConstantExport: true }],

      // Engineering rule 5: explicit, intentional types.
      '@typescript-eslint/consistent-type-imports': [
        'error',
        { prefer: 'type-imports', fixStyle: 'inline-type-imports' },
      ],
      '@typescript-eslint/no-unused-vars': [
        'error',
        { argsIgnorePattern: '^_', varsIgnorePattern: '^_' },
      ],

      // Engineering rule 6: never silently swallow errors.
      'no-empty': ['error', { allowEmptyCatch: false }],
      '@typescript-eslint/no-floating-promises': 'error',
      '@typescript-eslint/no-misused-promises': 'error',

      // `console.log` left in shipped UI code is noise; warn and error are
      // legitimate for genuine failures.
      'no-console': ['error', { allow: ['warn', 'error'] }],

      '@typescript-eslint/restrict-template-expressions': [
        'error',
        { allowNumber: true, allowBoolean: false, allowNullish: false },
      ],

      // This codebase uses `type` aliases throughout, because most component
      // prop types are intersections (`ComponentProps<'div'> &
      // VariantProps<...>`) which `interface` cannot express. Mandating
      // `interface` for the remaining plain shapes would make the style
      // inconsistent rather than uniform.
      '@typescript-eslint/consistent-type-definitions': 'off',
    },
  },

  // --- Design-system primitives ---
  {
    files: ['src/components/ui/**/*.tsx'],
    rules: {
      // These modules intentionally export a component alongside its `cva`
      // variant function or label map, which is the primitive's public API.
      // The cost is a full reload instead of fast refresh when editing a
      // primitive — not worth splitting every primitive across two files.
      'react-refresh/only-export-components': 'off',
    },
  },

  // --- Tests: assert against DOM shapes, use deliberate non-null assertions ---
  //
  // `e2e/**` is included because Playwright specs are tests under a different
  // suffix, and the same reasoning applies: a non-null assertion on a locator
  // that was just asserted visible documents intent better than a branch that
  // can never be taken.
  {
    files: ['**/*.test.{ts,tsx}', '**/*.spec.{ts,tsx}', 'e2e/**/*.ts', 'vitest.setup.ts'],
    languageOptions: {
      globals: { ...globals.node },
    },
    rules: {
      '@typescript-eslint/no-non-null-assertion': 'off',
      '@typescript-eslint/no-empty-function': 'off',
      '@typescript-eslint/no-extraneous-class': 'off',
    },
  },

  // --- Plain JS config files: no TypeScript program exists for these ---
  {
    files: ['**/*.{js,mjs,cjs}'],
    extends: [tseslint.configs.disableTypeChecked],
    languageOptions: {
      globals: { ...globals.node },
    },
  },
);
