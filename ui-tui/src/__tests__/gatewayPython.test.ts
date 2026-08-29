import { existsSync, mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'

import { afterEach, describe, expect, it, vi } from 'vitest'

import { resolvePython } from '../gatewayClient.js'

const tempRoots: string[] = []

afterEach(() => {
  vi.unstubAllEnvs()

  for (const root of tempRoots.splice(0)) {
    if (existsSync(root)) {
      rmSync(root, { force: true, recursive: true })
    }
  }
})

describe('Python interpreter resolution', () => {
  it('finds a repository-local Windows virtual environment', () => {
    vi.stubEnv('HERMES_PYTHON', '')
    vi.stubEnv('PYTHON', '')
    vi.stubEnv('VIRTUAL_ENV', '')

    const root = mkdtempSync(join(tmpdir(), 'hermes-tui-python-'))
    tempRoots.push(root)

    const scripts = resolve(root, '.venv/Scripts')
    const python = resolve(scripts, 'python.exe')
    mkdirSync(scripts, { recursive: true })
    writeFileSync(python, '')

    expect(resolvePython(root)).toBe(python)
  })
})
