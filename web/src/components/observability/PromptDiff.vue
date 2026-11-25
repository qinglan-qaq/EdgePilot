<script setup lang="ts">
import { computed } from 'vue'

/**
 * 行级 diff 展示：按行做 LCS 比较，不引入第三方库。
 *
 * - 相同行：普通样式；
 * - 右版本新增行：绿色；
 * - 左版本删除行：红色。
 *
 * 任一版本为 null / undefined 时显示“未记录”，绝不猜测内容。
 */

const props = withDefaults(
  defineProps<{
    /** 左侧（旧）文本 */
    left: string | null | undefined
    /** 右侧（新）文本 */
    right: string | null | undefined
    leftLabel?: string
    rightLabel?: string
    /** 空行占位文案 */
    emptyText?: string
  }>(),
  {
    leftLabel: '版本 A',
    rightLabel: '版本 B',
    emptyText: '未记录',
  },
)

/** 单行 diff 操作；equal 表示两版本相同。 */
interface DiffLine {
  type: 'equal' | 'add' | 'remove'
  text: string
}

const leftMissing = computed(() => props.left === null || props.left === undefined)
const rightMissing = computed(() => props.right === null || props.right === undefined)
const bothMissing = computed(() => leftMissing.value && rightMissing.value)

function toLines(value: string | null | undefined): string[] {
  if (value === null || value === undefined) {
    return []
  }
  // 空字符串视为没有行，而不是产生一条空行参与 diff。
  if (value === '') {
    return []
  }
  return value.replace(/\r\n/g, '\n').replace(/\r/g, '\n').split('\n')
}

/** 经典 LCS：先算出最长公共子序列长度，再回放得到行级操作。 */
function diffLines(oldLines: string[], newLines: string[]): DiffLine[] {
  const oldCount = oldLines.length
  const newCount = newLines.length
  const table: number[][] = Array.from({ length: oldCount + 1 }, () =>
    new Array<number>(newCount + 1).fill(0),
  )

  for (let i = oldCount - 1; i >= 0; i -= 1) {
    for (let j = newCount - 1; j >= 0; j -= 1) {
      table[i][j] =
        oldLines[i] === newLines[j]
          ? table[i + 1][j + 1] + 1
          : Math.max(table[i + 1][j], table[i][j + 1])
    }
  }

  const result: DiffLine[] = []
  let i = 0
  let j = 0
  while (i < oldCount && j < newCount) {
    if (oldLines[i] === newLines[j]) {
      result.push({ type: 'equal', text: oldLines[i] })
      i += 1
      j += 1
    } else if (table[i + 1][j] >= table[i][j + 1]) {
      result.push({ type: 'remove', text: oldLines[i] })
      i += 1
    } else {
      result.push({ type: 'add', text: newLines[j] })
      j += 1
    }
  }
  while (i < oldCount) {
    result.push({ type: 'remove', text: oldLines[i] })
    i += 1
  }
  while (j < newCount) {
    result.push({ type: 'add', text: newLines[j] })
    j += 1
  }
  return result
}

const lines = computed<DiffLine[]>(() =>
  diffLines(toLines(props.left), toLines(props.right)),
)

const hasChanges = computed(() => lines.value.some((line) => line.type !== 'equal'))

function displayText(line: DiffLine): string {
  if (line.text === '') {
    return props.emptyText
  }
  return line.text
}
</script>

<template>
  <div class="prompt-diff" data-test="prompt-diff">
    <div class="prompt-diff__header">
      <span class="prompt-diff__label prompt-diff__label--left">{{ leftLabel }}</span>
      <span class="prompt-diff__label prompt-diff__label--right">{{ rightLabel }}</span>
    </div>

    <p v-if="bothMissing" class="prompt-diff__missing" data-test="prompt-diff-empty">
      {{ emptyText }}
    </p>

    <template v-else>
      <p
        v-if="leftMissing"
        class="prompt-diff__missing"
        data-test="prompt-diff-missing-left"
      >
        左侧{{ emptyText }}
      </p>
      <p
        v-if="rightMissing"
        class="prompt-diff__missing"
        data-test="prompt-diff-missing-right"
      >
        右侧{{ emptyText }}
      </p>

      <div class="prompt-diff__lines" data-test="prompt-diff-lines">
        <div
          v-for="(line, index) in lines"
          :key="`${line.type}-${index}`"
          class="prompt-diff__line"
          :class="`prompt-diff__line--${line.type}`"
          data-test="diff-line"
          :data-kind="line.type"
        >
          <span class="prompt-diff__sign" aria-hidden="true">
            {{ line.type === 'add' ? '+' : line.type === 'remove' ? '-' : ' ' }}
          </span>
          <span
            class="prompt-diff__text"
            :class="{ 'prompt-diff__text--empty': line.text === '' }"
            >{{ displayText(line) }}</span
          >
        </div>
      </div>

      <p v-if="!hasChanges" class="prompt-diff__same" data-test="prompt-diff-same">
        两个版本内容一致
      </p>
    </template>
  </div>
</template>

<style scoped>
.prompt-diff {
  border: 1px solid #e5e7eb;
  border-radius: 8px;
  overflow: hidden;
  background: #ffffff;
}

.prompt-diff__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 8px 12px;
  background: #f8fafc;
  border-bottom: 1px solid #eef1f6;
  font-size: 12px;
}

.prompt-diff__label--left {
  color: #b91c1c;
}

.prompt-diff__label--right {
  color: #15803d;
}

.prompt-diff__lines {
  max-height: 320px;
  overflow: auto;
}

.prompt-diff__line {
  display: flex;
  gap: 8px;
  padding: 1px 12px;
  font-family: 'JetBrains Mono', Consolas, monospace;
  font-size: 12px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
}

.prompt-diff__line--equal {
  color: #334155;
}

.prompt-diff__line--add {
  background: #dcfce7;
  color: #166534;
}

.prompt-diff__line--remove {
  background: #fee2e2;
  color: #991b1b;
}

.prompt-diff__sign {
  flex-shrink: 0;
  width: 10px;
  color: inherit;
  opacity: 0.7;
}

.prompt-diff__text {
  flex: 1;
}

.prompt-diff__text--empty {
  color: #94a3b8;
  font-style: italic;
}

.prompt-diff__missing,
.prompt-diff__same {
  margin: 0;
  padding: 10px 12px;
  color: #94a3b8;
  font-size: 12px;
}
</style>
