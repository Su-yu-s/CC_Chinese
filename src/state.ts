// 六态状态机 + 前端交互逻辑
import { ref } from 'vue'
import { detect, install, restore, openClaude, type DetectStatus, type OpResult } from './lib/tauri'

export type AppState =
  | 'idle'        // 待汉化
  | 'installing'  // 汉化中
  | 'restoring'   // 恢复中
  | 'localized'   // 已汉化
  | 'missing'     // 未找到
  | 'detecting'   // 检测中

/** 语义色调：对应 glass.css 的状态色 */
export type Tone = 'accent' | 'accent2' | 'ok' | 'warn' | 'danger' | 'neutral'

const STATE_MAP: Record<
  AppState,
  { tone: Tone; label: string; primaryBtn: string; enabled: boolean }
> = {
  idle:       { tone: 'warn',    label: '待汉化', primaryBtn: '一键汉化',       enabled: true },
  installing: { tone: 'accent',  label: '汉化中', primaryBtn: '汉化中...',      enabled: false },
  restoring:  { tone: 'accent2', label: '恢复中', primaryBtn: '恢复中...',      enabled: false },
  localized:  { tone: 'ok',      label: '已汉化', primaryBtn: '打开 Claude',    enabled: true },
  missing:    { tone: 'danger',  label: '未找到', primaryBtn: '重新检测',       enabled: true },
  detecting:  { tone: 'neutral', label: '检测中', primaryBtn: '检测中...',      enabled: false },
}

export const state = ref<AppState>('detecting')
export const statusInfo = ref<{ version: string | null; isWindowsApps: boolean; appDir: string | null }>({
  version: null,
  isWindowsApps: false,
  appDir: null,
})
export const busy = ref(false)
export const lastResult = ref<OpResult | null>(null)

/** 状态色 CSS 变量值 */
export function toneColor(tone: Tone): string {
  switch (tone) {
    case 'accent': return 'var(--accent)'
    case 'accent2': return 'var(--accent-2)'
    case 'ok': return 'var(--ok)'
    case 'warn': return 'var(--warn)'
    case 'danger': return 'var(--danger)'
    default: return 'var(--ink-3)'
  }
}

export function ui() {
  return STATE_MAP[state.value]
}

export async function refreshStatus() {
  state.value = 'detecting'
  try {
    const s: DetectStatus = await detect()
    statusInfo.value = {
      version: s.version,
      isWindowsApps: s.is_windowsapps,
      appDir: s.app_dir,
    }
    state.value = s.state === 'ready' ? 'localized' : s.state === 'repair' ? 'idle' : 'missing'
  } catch (e) {
    console.error(e)
    state.value = 'missing'
  }
}

async function runCommand(fn: () => Promise<OpResult>) {
  busy.value = true
  try {
    const result = await fn()
    lastResult.value = result
    await refreshStatus()
    return result
  } finally {
    busy.value = false
  }
}

export async function onPrimary() {
  if (busy.value) return
  if (state.value === 'localized') {
    if (!statusInfo.value.appDir) return
    state.value = 'detecting'
    await runCommand(() => openClaude(statusInfo.value.appDir!))
  } else if (state.value === 'idle') {
    if (!statusInfo.value.appDir) return
    state.value = 'installing'
    const elevated = statusInfo.value.isWindowsApps
    await runCommand(() => install(statusInfo.value.appDir!, elevated))
  } else if (state.value === 'missing') {
    await refreshStatus()
  }
}

export async function onRestore() {
  if (busy.value || !statusInfo.value.appDir) return
  const elevated = statusInfo.value.isWindowsApps
  state.value = 'restoring'
  await runCommand(() => restore(statusInfo.value.appDir!, elevated))
}

// 初始化：自动检测
refreshStatus()
