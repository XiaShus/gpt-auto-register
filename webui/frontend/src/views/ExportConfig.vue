<script setup>
import { computed, onActivated, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  listPushTargets,
  createPushTarget,
  updatePushTarget,
  deletePushTarget,
  testPushTarget,
  retryPendingPush,
} from '@/api/settings'
import FooterToolbar from '@/components/FooterToolbar.vue'

const targets = ref([])
const types = ref([])
const loading = ref(false)
const savingIds = ref(new Set())
const testingIds = ref(new Set())
const retrying = ref(false)
const draft = reactive({ name: '', type: 'chatgpt2api', url: '', credential: '', groupId: '', weight: 1, timeout: 60 })

const enabledCount = computed(() => targets.value.filter((t) => t.enabled).length)
const totalWeight = computed(() => targets.value.filter((t) => t.enabled).reduce((s, t) => s + (t.weight || 1), 0))

// 前端只做展示：真正按权重抽签发生在后端 pick_push_target()
function share(target) {
  if (!target.enabled || totalWeight.value <= 0) return '—'
  return `${Math.round(((target.weight || 1) / totalWeight.value) * 100)}%`
}

async function load() {
  loading.value = true
  try {
    const res = await listPushTargets()
    targets.value = (res.targets || []).map((t) => ({ ...t, credential: '' }))
    types.value = res.types || []
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    loading.value = false
  }
}

async function addTarget() {
  if (!draft.type || !draft.url.trim()) {
    ElMessage.warning('至少填写类型和地址')
    return
  }
  try {
    await createPushTarget({
      name: draft.name.trim() || draft.type,
      type: draft.type,
      url: draft.url.trim(),
      credential: draft.credential.trim(),
      group_id: draft.groupId.trim(),
      weight: Number(draft.weight) || 1,
      enabled: true,
      timeout: Number(draft.timeout) || 60,
    })
    ElMessage.success('已添加')
    draft.name = ''
    draft.url = ''
    draft.credential = ''
    draft.groupId = ''
    draft.weight = 1
    load()
  } catch (e) {
    ElMessage.error(e.message)
  }
}

async function saveTarget(target) {
  savingIds.value = new Set(savingIds.value).add(target.id)
  try {
    // credential 为空表示「不修改」，后端对空串会跳过；这里显式传 *** 语义更清楚
    await updatePushTarget(target.id, {
      name: target.name,
      type: target.type,
      url: target.url,
      credential: target.credential ? target.credential : '***',
      group_id: target.group_id || '',
      weight: Number(target.weight) || 1,
      enabled: !!target.enabled,
      timeout: Number(target.timeout) || 60,
    })
    ElMessage.success('已保存')
    load()
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    const next = new Set(savingIds.value)
    next.delete(target.id)
    savingIds.value = next
  }
}

async function removeTarget(target) {
  try {
    await ElMessageBox.confirm(`删除推送目标「${target.name}」？`, '删除确认', { type: 'warning' })
  } catch {
    return
  }
  try {
    await deletePushTarget(target.id)
    ElMessage.success('已删除')
    load()
  } catch (e) {
    ElMessage.error(e.message)
  }
}

async function testTarget(target) {
  testingIds.value = new Set(testingIds.value).add(target.id)
  try {
    const res = await testPushTarget(target.id)
    if (res.ok) ElMessage.success('连通正常')
    else ElMessage.error(res.result?.error || '连通失败')
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    const next = new Set(testingIds.value)
    next.delete(target.id)
    testingIds.value = next
  }
}

async function retryPending() {
  retrying.value = true
  try {
    const res = await retryPendingPush({ limit: 50 })
    if (!res.attempted) ElMessage.info('没有待重推的账号')
    else ElMessage.success(`尝试 ${res.attempted} 个，成功 ${res.succeeded} 个`)
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    retrying.value = false
  }
}

onActivated(() => load())
load()
</script>

<template>
  <div class="page">
    <el-card shadow="never">
      <template #header>
        <span class="section-title" style="margin: 0">推送渠道</span>
        <el-tag size="small" style="margin-left: 8px">启用 {{ enabledCount }} / 共 {{ targets.length }}</el-tag>
      </template>

      <p class="hint">
        每次注册成功按<b>权重</b>随机选 <b>1 个</b>启用中的渠道推送；全为 1 即 1:1。推送失败不会丢号，
        账号会留在这里的「待重推」里，可批量补推。
      </p>
      <p class="hint" style="color: var(--el-color-danger); font-weight: 600">
        注意：只有带 <b>RT（refresh_token）</b>的账号，推过去才能正常使用；CPA / SUB2API 还需要用 RT 换 Codex token。
      </p>

      <el-table :data="targets" v-loading="loading" size="small" style="width: 100%">
        <el-table-column label="启用" width="70">
          <template #default="{ row }">
            <el-switch v-model="row.enabled" />
          </template>
        </el-table-column>
        <el-table-column label="名称" min-width="130">
          <template #default="{ row }">
            <el-input v-model="row.name" size="small" />
          </template>
        </el-table-column>
        <el-table-column label="类型" width="160">
          <template #default="{ row }">
            <el-select v-model="row.type" size="small">
              <el-option v-for="t in types" :key="t.value" :label="t.label" :value="t.value" />
            </el-select>
          </template>
        </el-table-column>
        <el-table-column label="地址" min-width="220">
          <template #default="{ row }">
            <el-input v-model="row.url" size="small" placeholder="https://..." />
          </template>
        </el-table-column>
        <el-table-column label="密钥" min-width="150">
          <template #default="{ row }">
            <el-input
              v-model="row.credential"
              size="small"
              type="password"
              show-password
              :placeholder="row.has_credential ? '已设置（留空不修改）' : '粘贴密钥'"
            />
          </template>
        </el-table-column>
        <el-table-column label="分组" width="110">
          <template #default="{ row }">
            <el-input v-model="row.group_id" size="small" placeholder="可选" />
          </template>
        </el-table-column>
        <el-table-column label="权重" width="120">
          <template #default="{ row }">
            <el-input-number v-model="row.weight" :min="1" :max="100" size="small" controls-position="right" />
          </template>
        </el-table-column>
        <el-table-column label="实际占比" width="90">
          <template #default="{ row }">{{ share(row) }}</template>
        </el-table-column>
        <el-table-column label="超时" width="110">
          <template #default="{ row }">
            <el-input-number v-model="row.timeout" :min="5" :max="600" size="small" controls-position="right" />
          </template>
        </el-table-column>
        <el-table-column label="失败" width="70">
          <template #default="{ row }">
            <el-tag :type="row.fail_count ? 'danger' : 'info'" size="small">{{ row.fail_count }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="200" fixed="right">
          <template #default="{ row }">
            <el-button size="small" :loading="savingIds.has(row.id)" @click="saveTarget(row)">保存</el-button>
            <el-button size="small" :loading="testingIds.has(row.id)" @click="testTarget(row)">测试</el-button>
            <el-button size="small" type="danger" link @click="removeTarget(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>

      <el-divider content-position="left">新增渠道</el-divider>
      <el-form label-position="top" style="max-width: 920px">
        <el-row :gutter="12">
          <el-col :span="6">
            <el-form-item label="名称">
              <el-input v-model="draft.name" placeholder="例如 pool-154" />
            </el-form-item>
          </el-col>
          <el-col :span="6">
            <el-form-item label="类型">
              <el-select v-model="draft.type" style="width: 100%">
                <el-option v-for="t in types" :key="t.value" :label="t.label" :value="t.value" />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col :span="8">
            <el-form-item label="地址">
              <el-input v-model="draft.url" placeholder="https://pool.example.com" />
            </el-form-item>
          </el-col>
          <el-col :span="4">
            <el-form-item label="权重">
              <el-input-number v-model="draft.weight" :min="1" :max="100" style="width: 100%" controls-position="right" />
            </el-form-item>
          </el-col>
        </el-row>
        <el-row :gutter="12">
          <el-col :span="9">
            <el-form-item label="密钥">
              <el-input v-model="draft.credential" type="password" show-password placeholder="管理密钥 / 管理员 Key" />
            </el-form-item>
          </el-col>
          <el-col :span="5">
            <el-form-item label="分组（可选）">
              <el-input v-model="draft.groupId" />
            </el-form-item>
          </el-col>
          <el-col :span="4">
            <el-form-item label="超时 (秒)">
              <el-input-number v-model="draft.timeout" :min="5" :max="600" style="width: 100%" controls-position="right" />
            </el-form-item>
          </el-col>
          <el-col :span="6">
            <el-form-item label=" ">
              <el-button type="primary" style="width: 100%" @click="addTarget">添加到列表</el-button>
            </el-form-item>
          </el-col>
        </el-row>
      </el-form>
    </el-card>

    <FooterToolbar>
      <template #left>
        启用 {{ enabledCount }} 个渠道 · 权重合计 {{ totalWeight }}
      </template>
      <el-button :loading="retrying" @click="retryPending">重推失败账号</el-button>
      <el-button @click="load">刷新</el-button>
    </FooterToolbar>
  </div>
</template>
