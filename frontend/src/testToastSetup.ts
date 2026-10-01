// 组件测试复用真实提示设施，结束时通过组件卸载清理焦点与计时器。
import { beforeEach, afterEach } from 'vitest'
import { enableAutoUnmount } from '@vue/test-utils'
import { clearToasts } from './composables/useToast'

enableAutoUnmount(afterEach)
beforeEach(clearToasts)
