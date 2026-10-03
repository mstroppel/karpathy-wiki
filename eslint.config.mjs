import js from '@eslint/js'
import globals from 'globals'

export default [
  { ignores: ['node_modules/', 'temp/', '.venv/'] },
  js.configs.recommended,
  {
    files: [
      'config/**/*.js',
      'config/**/*.mjs',
      'openchamber/**/*.mjs',
      'tests/**/*.mjs',
      'eslint.config.mjs',
    ],
    languageOptions: {
      ecmaVersion: 2024,
      sourceType: 'module',
      globals: {
        ...globals.node,
      },
    },
  },
]
