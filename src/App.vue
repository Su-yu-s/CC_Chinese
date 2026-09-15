<template>
  <div class="ambient"><i /></div>

  <div class="shell">
    <!-- 标题栏：拖拽区 + 品牌 + 状态胶囊 + 主题/窗口按钮 -->
    <header class="titlebar" data-tauri-drag-region>
      <div class="brand" data-tauri-drag-region>
        <img class="brand-mark" src="./assets/icon.png" alt="" draggable="false" />
        <span class="brand-name">CC_Chinese</span>
      </div>

      <div class="pill glass-thin" :style="{ '--dot': toneColor(ui().tone) }">
        <span class="dot" />
        {{ ui().label }}
      </div>

      <div class="spacer" data-tauri-drag-region />

      <div class="win-buttons" v-if="inTauri">
        <button class="btn-icon" aria-label="最小化" @click="win!.minimize()">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
               stroke-linecap="round"><path d="M5 12h14" /></svg>
        </button>
        <button class="btn-icon danger" aria-label="关闭" @click="win!.close()">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
               stroke-linecap="round"><path d="m6 6 12 12M18 6 6 18" /></svg>
        </button>
      </div>
    </header>

    <!-- 主内容：空白区域可拖动窗口 -->
    <main class="main" data-tauri-drag-region>
      <!-- 状态图标：玻璃圆环 + 描边 SVG -->
      <div class="halo" :style="{ '--dot': toneColor(ui().tone) }" aria-hidden="true">
        <div class="halo-core glass" :class="{ working: !ui().enabled }">
          <svg class="halo-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor"
               stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
            <template v-if="state === 'detecting'">
              <circle cx="11" cy="11" r="7" />
              <path d="m16.5 16.5 4.5 4.5" />
            </template>
            <template v-else-if="state === 'idle'">
              <path d="M12 3l1.9 5.8a2 2 0 0 0 1.3 1.3L21 12l-5.8 1.9a2 2 0 0 0-1.3 1.3L12 21l-1.9-5.8a2 2 0 0 0-1.3-1.3L3 12l5.8-1.9a2 2 0 0 0 1.3-1.3Z" />
            </template>
            <template v-else-if="state === 'installing'">
              <path d="M17 3a2.85 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z" />
            </template>
            <template v-else-if="state === 'restoring'">
              <path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8" />
              <path d="M3 3v5h5" />
            </template>
            <template v-else-if="state === 'localized'">
              <circle cx="12" cy="12" r="9" />
              <path d="m8.5 12.2 2.4 2.4 4.8-5" />
            </template>
            <template v-else>
              <circle cx="12" cy="12" r="9" />
              <path d="m9 9 6 6M15 9l-6 6" />
            </template>
          </svg>
        </div>
      </div>

      <p class="headline">{{ headline }}</p>
      <p class="sub" v-if="sub">{{ sub }}</p>

      <!-- 元信息卡 -->
      <div class="meta glass-thin" v-if="statusInfo.appDir">
        <div class="meta-row" v-if="statusInfo.version">
          <span class="k">版本</span><span class="v">{{ statusInfo.version }}</span>
        </div>
        <div class="meta-row">
          <span class="k">位置</span>
          <span class="v path" :title="statusInfo.appDir">{{ statusInfo.appDir }}</span>
        </div>
        <div class="meta-row" v-if="statusInfo.isWindowsApps">
          <span class="k">来源</span><span class="v">Microsoft Store（WindowsApps）</span>
        </div>
      </div>

      <!-- 不确定进度条 -->
      <div class="meter" v-if="!ui().enabled && state !== 'detecting'" aria-hidden="true">
        <i />
      </div>

      <!-- 主按钮 -->
      <button
        class="btn btn-primary cta"
        :disabled="!ui().enabled"
        @click="onPrimary"
      >
        {{ ui().primaryBtn }}
      </button>

      <!-- 恢复按钮（仅已汉化时） -->
      <button class="btn ghost" v-if="state === 'localized'" @click="onRestore">
        恢复原样
      </button>
    </main>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { getCurrentWindow } from '@tauri-apps/api/window'
import {
  state, statusInfo, ui, toneColor, onPrimary, onRestore,
} from './state'

const inTauri = '__TAURI_INTERNALS__' in window
const win = inTauri ? getCurrentWindow() : null

const HEADLINES: Record<string, string> = {
  idle: '可以安装中文界面',
  installing: '正在写入中文资源…',
  restoring: '正在恢复官方原状…',
  localized: 'Claude 已是中文界面',
  missing: '没有找到 Claude Desktop',
  detecting: '正在检测 Claude Desktop…',
}

const headline = computed(() => HEADLINES[state.value] ?? '')

const sub = computed(() => {
  switch (state.value) {
    case 'idle': return '操作会先封存官方基线，失败自动回滚。'
    case 'missing': return '安装 Claude Desktop 后点击重新检测。'
    case 'localized': return '需要还原时随时可以恢复。'
    default: return ''
  }
})
</script>

<style scoped>
.shell {
  position: relative;
  z-index: 1;
  height: 100%;
  display: flex;
  flex-direction: column;
}

/* 标题栏纯展示元素：点击穿透到拖拽层 */
.pill,
.halo,
.halo-core,
.headline,
.sub,
.meta,
.meter {
  pointer-events: none;
}

/* ---------- 标题栏 ---------- */
.titlebar {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 12px 6px;
  flex-shrink: 0;
}
.brand {
  display: flex;
  align-items: center;
  gap: 7px;
}
.brand-mark {
  pointer-events: none;
  width: 24px; height: 24px;
  border-radius: 7px;
  box-shadow: 0 2px 8px -2px rgba(29, 29, 31, 0.35);
}
.brand-name {
  pointer-events: none;
  font-size: 13px;
  font-weight: 700;
  letter-spacing: 0.01em;
}
.win-buttons { display: flex; gap: 2px; }

/* ---------- 主区 ---------- */
.main {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 10px;
  padding: 0 24px 18px;
  min-height: 0;
}

/* 状态玻璃圆环 */
.halo { position: relative; }
.halo::before {
  content: "";
  position: absolute;
  inset: -14px;
  border-radius: 50%;
  background: radial-gradient(circle, var(--dot) 0%, transparent 70%);
  opacity: 0.28;
  filter: blur(14px);
}
.halo-core {
  position: relative;
  width: 84px; height: 84px;
  border-radius: 50%;
  display: grid;
  place-items: center;
}
.halo-icon {
  width: 36px; height: 36px;
  color: var(--dot);
}
.halo-core.working .halo-icon {
  animation: breathe 1.6s var(--ease) infinite alternate;
}
@keyframes breathe { to { transform: scale(1.12); opacity: 0.75; } }
@media (prefers-reduced-motion: reduce) {
  .halo-core.working .halo-icon { animation: none; }
}

.headline {
  margin: 4px 0 0;
  font-size: 17px;
  font-weight: 700;
  text-align: center;
}
.sub {
  margin: 0;
  font-size: 12px;
  color: var(--ink-2);
  text-align: center;
}

/* 元信息卡 */
.meta {
  width: 100%;
  border-radius: var(--r-md);
  padding: 8px 12px;
  display: flex;
  flex-direction: column;
  gap: 4px;
  max-width: 300px;
}
.meta-row {
  display: flex;
  gap: 8px;
  font-size: 11.5px;
  line-height: 1.5;
  min-width: 0;
}
.meta-row .k {
  color: var(--ink-3);
  flex: none;
  font-weight: 600;
}
.meta-row .v {
  color: var(--ink-2);
  min-width: 0;
}
.path {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  direction: rtl; /* 长路径截头留尾 */
  text-align: left;
}

/* 不确定进度条 */
.meter {
  width: 160px;
  height: 4px;
  border-radius: 2px;
  background: var(--shade);
  overflow: hidden;
  position: relative;
}
.meter i {
  position: absolute;
  top: 0; left: -40%;
  width: 40%; height: 100%;
  border-radius: 2px;
  background: var(--accent-grad);
  animation: slide 1.2s var(--ease) infinite;
}
@keyframes slide { to { left: 100%; } }
@media (prefers-reduced-motion: reduce) {
  .meter i { animation: none; left: 30%; }
}

/* 按钮 */
.cta {
  width: 100%;
  max-width: 300px;
  height: 40px;
  font-size: 14px;
  border-radius: var(--r-md);
}
.ghost {
  width: 100%;
  max-width: 300px;
  height: 34px;
  font-size: 12.5px;
  color: var(--ink-2);
  border-radius: var(--r-md);
}
.ghost:hover { color: var(--ink); }
</style>
