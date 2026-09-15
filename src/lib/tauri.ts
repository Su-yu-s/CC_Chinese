// Tauri 命令桥接层
import { invoke } from '@tauri-apps/api/core'

export type DetectStatus = {
  state: string
  message: string
  version: string | null
  is_windowsapps: boolean
  app_dir: string | null
}

export type OpResult = {
  success: boolean
  state: string
  message: string
  error_code?: string | null
}

export function detect(target?: string): Promise<DetectStatus> {
  return invoke('detect', { target: target ?? null })
}

export function install(appDir: string, elevated: boolean): Promise<OpResult> {
  return invoke('install', { appDir, elevated })
}

export function restore(appDir: string, elevated: boolean): Promise<OpResult> {
  return invoke('restore', { appDir, elevated })
}

export function openClaude(appDir: string): Promise<OpResult> {
  return invoke('open-claude', { appDir })
}
