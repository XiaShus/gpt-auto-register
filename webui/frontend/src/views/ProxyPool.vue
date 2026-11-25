<script setup>
import { computed, ref } from 'vue'
import { storeToRefs } from 'pinia'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useProxyStore, isValidProxy, proxyScheme } from '@/stores/proxy'
import { useFormStore } from '@/stores/form'
import { testProxies, fetchZooproxy } from '@/api/proxy'
import { copyText } from '@/api/request'

const proxyStore = useProxyStore()
const { form } = storeToRefs(useFormStore())
const { list, count } = storeToRefs(proxyStore)

const draft = ref('')
const testResults = ref({}) // proxy -> { status:'testing'|'ok'|'fail', latency_ms, ip, error }
const testingAll = ref(false)
const zooTesting = ref(false)
const zooLast = ref('')

async function testZooproxy() {
  const url = (form.value.zooproxyApiUrl || '').trim()
  if (!url) {
    ElMessage.warning('请先在全自动/单次页配置 ZooProxy API URL，或在此填写')
    return
  }
  zooTesting.value = true
  zooLast.value = ''
  try {
    const { ok, proxy, error } = await fetchZooproxy(url, form.value.zooproxyScheme || 'http')
    if (!ok) {
      ElMessage.error(error || '提取失败')
      zooLast.value = error || '失败'
      return
    }
    zooLast.value = proxy
    ElMessage.success('提取成功: ' + proxy)
  } catch (e) {
    ElMessage.error(e.message)
    zooLast.value = e.message
  } finally {
    zooTesting.value = false
  }
}

const rows = computed(() =>
  list.value.map((p, i) => ({
    index: i + 1, proxy: p, valid: isValidProxy(p), result: testResults.value[p] || null,
  })),
)
const invalidCount = computed(() => rows.value.filter((r) => !r.valid).length)

async function runTest(targets) {
  if (!targets.length) return
  for (const p of targets) testResults.value[p] = { status: 'testing' }
  try {
    const { results } = await testProxies(targets)
    for (const [proxy, res] of Object.entries(results)) {
      testResults.value[proxy] = { status: res.ok ? 'ok' : 'fail', ...res }
    }
  } catch (e) {
    for (const p of targets) testResults.value[p] = { status: 'fail', error: e.message }
    ElMessage.error('测试失败: ' + e.message)
  }
}
async function testOne(proxy) {
  await runTest([proxy])
}
async function testAll() {
  if (!count.value) return
  testingAll.value = true
  try { await runTest([...list.value]) }
  finally { testingAll.value = false }
}

function save() {
  if (!draft.value.trim()) { ElMessage.warning('请先粘贴代理'); return }
  const r = proxyStore.setFromText(draft.value)
  draft.value = ''
  ElMessage.success(`已保存 ${r.kept} 个代理${r.duplicated ? `（去重 ${r.duplicated} 个）` : ''}`)
}
function append() {
  if (!draft.value.trim()) { ElMessage.warning('请先粘贴代理'); return }
  const r = proxyStore.append(draft.value)
  draft.value = ''
  ElMessage.success(`已追加 ${r.added} 个新代理`)
}
async function clearAll() {
  if (!count.value) return
  try {
    await ElMessageBox.confirm(`确定清空全部 ${count.value} 个代理？`, '确认', { type: 'warning', confirmButtonText: '确定', cancelButtonText: '取消' })
    proxyStore.clear()
    ElMessage.success('已清空')
  } catch (_) { /* cancel */ }
}
function editInDraft() {
  draft.value = proxyStore.text
  ElMessage.info('已把当前代理池载入编辑框，改完点「覆盖保存」')
}
</script>

<template>
  <div class="page">
    <el-row :gutter="16">
      <el-col :span="24" style="margin-bottom: 16px">
        <el-card shadow="never">
          <template #header><span class="section-title" style="margin: 0">ZooProxy 动态住宅（白名单 API）</span></template>
          <p class="hint">
            在 <a href="https://console.zooproxy.com/traffic" target="_blank" rel="noopener">ZooProxy 控制台</a>
            复制「使用链接」，并把 VPS 出口 IP 加入白名单。API 返回 JSON 时建议
            <span class="mono">num=1</span>（每号一条 IP）。
          </p>
          <el-input v-model="form.zooproxyApiUrl" class="mono" placeholder="https://white.zooproxy.com/white/api?..." />
          <div style="margin-top: 10px; display: flex; gap: 8px; flex-wrap: wrap; align-items: center">
            <el-select v-model="form.zooproxyScheme" style="width: 140px">
              <el-option label="http" value="http" />
              <el-option label="https" value="https" />
              <el-option label="socks5" value="socks5" />
            </el-select>
            <el-button type="primary" :loading="zooTesting" @click="testZooproxy">试拉一条 IP</el-button>
            <el-button v-if="zooLast" size="small" @click="copyText(zooLast)">复制结果</el-button>
          </div>
          <p v-if="zooLast" class="hint mono" style="margin-top: 8px">{{ zooLast }}</p>
        </el-card>
      </el-col>
      <el-col :md="10" style="margin-bottom: 16px">
        <el-card shadow="never">
          <template #header><span class="section-title" style="margin: 0">批量导入</span></template>
          <p class="hint">
            每行一个：<span class="mono">[协议://][user:pass@]host:port</span><br />
            不写协议默认按 <b>HTTP 代理</b>；SOCKS5 必须写 <span class="mono">socks5://</span>。<br />
            若某代理裸写能连、加了 <span class="mono">socks5://</span> 反而连不上，说明它其实是 HTTP 代理。
          </p>
          <el-input
            v-model="draft" type="textarea" :rows="12" class="mono"
            placeholder="socks5://127.0.0.1:7890&#10;socks5://user:pass@1.2.3.4:1080&#10;http://5.6.7.8:8080"
          />
          <div style="margin-top: 12px; display: flex; gap: 8px; flex-wrap: wrap">
            <el-button type="primary" @click="save">覆盖保存</el-button>
            <el-button @click="append">追加合并</el-button>
            <el-button @click="editInDraft">载入当前池</el-button>
          </div>
        </el-card>
      </el-col>

      <el-col :md="14" style="margin-bottom: 16px">
        <el-card shadow="never">
          <template #header>
            <div style="display: flex; align-items: center; justify-content: space-between">
              <span class="section-title" style="margin: 0">
                当前代理池（{{ count }} 个<template v-if="invalidCount">，<span style="color: var(--el-color-danger)">{{ invalidCount }} 个格式异常</span></template>）
              </span>
              <div style="display: flex; gap: 8px">
                <el-button size="small" type="primary" plain :loading="testingAll" :disabled="!count" @click="testAll">测试全部</el-button>
                <el-button size="small" :disabled="!count" @click="copyText(proxyStore.text)">复制全部</el-button>
                <el-button size="small" type="danger" plain :disabled="!count" @click="clearAll">清空</el-button>
              </div>
            </div>
          </template>

          <el-table :data="rows" size="small" stripe max-height="440">
            <el-table-column prop="index" label="#" width="48" />
            <el-table-column prop="proxy" label="代理地址" min-width="200" show-overflow-tooltip>
              <template #default="{ row }"><span class="mono">{{ row.proxy }}</span></template>
            </el-table-column>
            <el-table-column label="格式" width="70">
              <template #default="{ row }">
                <el-tag :type="row.valid ? 'success' : 'danger'" size="small" effect="light">
                  {{ row.valid ? '正常' : '异常' }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="生效协议" width="110">
              <template #default="{ row }">
                <span class="mono" style="font-size: 12px">{{ proxyScheme(row.proxy) }}</span>
              </template>
            </el-table-column>
            <el-table-column label="连通性" min-width="150">
              <template #default="{ row }">
                <template v-if="!row.result">
                  <span class="hint">未测</span>
                </template>
                <el-tag v-else-if="row.result.status === 'testing'" type="warning" size="small">测试中…</el-tag>
                <template v-else-if="row.result.status === 'ok'">
                  <el-tag type="success" size="small">正常 {{ row.result.latency_ms }}ms</el-tag>
                  <span v-if="row.result.ip" class="hint mono" style="margin-left: 6px">{{ row.result.ip }}</span>
                </template>
                <el-tooltip v-else :content="row.result.error || '连接失败'" placement="top">
                  <el-tag type="danger" size="small">失败</el-tag>
                </el-tooltip>
              </template>
            </el-table-column>
            <el-table-column label="操作" width="120" fixed="right">
              <template #default="{ row }">
                <el-button
                  size="small" text type="primary"
                  :loading="row.result && row.result.status === 'testing'"
                  @click="testOne(row.proxy)"
                >测试</el-button>
                <el-button size="small" text type="danger" @click="proxyStore.remove(row.proxy)">删除</el-button>
              </template>
            </el-table-column>
            <template #empty>暂无代理，请在左侧批量导入</template>
          </el-table>

          <el-alert
            type="info" :closable="false" show-icon style="margin-top: 12px"
            title="全自动批量跑号时，各 worker 会按顺序轮流取用这里的代理；代理池为空则所有 worker 用「单次注册」页填的单个代理。"
          />
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>
