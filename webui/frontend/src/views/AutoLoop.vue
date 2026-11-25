<script setup>
import { computed } from 'vue'
import { useRouter } from 'vue-router'
import { storeToRefs } from 'pinia'
import { ElMessage } from 'element-plus'
import { autoStart, autoPause, autoResume, autoStop } from '@/api/register'
import { useFormStore, proxyText } from '@/stores/form'
import { useProxyStore } from '@/stores/proxy'
import { useRuntimeStore } from '@/stores/runtime'
import LogPanel from '@/components/LogPanel.vue'
import StatusDot from '@/components/StatusDot.vue'

const router = useRouter()
const { form } = storeToRefs(useFormStore())
const proxyStore = useProxyStore()
const { count: proxyCount } = storeToRefs(proxyStore)
const runtime = useRuntimeStore()
const { autoStatus } = storeToRefs(runtime)

const st = computed(() => autoStatus.value.state || 'stopped')
const canStart = computed(() => st.value === 'stopped')
const canPause = computed(() => st.value === 'running')
const canResume = computed(() => st.value === 'paused')
const canStop = computed(() => st.value !== 'stopped')

const stateLabel = computed(() => ({
  stopped: '未运行', running: '运行中', paused: '已暂停',
}[st.value] || st.value))
const stateType = computed(() => ({
  stopped: 'info', running: 'success', paused: 'warning',
}[st.value] || 'info'))

const workers = computed(() => Array.isArray(autoStatus.value.workers) ? autoStatus.value.workers : [])

async function start() {
  try {
    await autoStart({
      proxy: proxyText(form.value),
      proxy_pool: proxyStore.text,
      concurrency: parseInt(form.value.autoConcurrency, 10) || 1,
      otp_timeout: parseInt(form.value.otpTimeout, 10) || 10,
      want_access_token: true,
      want_session_token: true,
      want_refresh_token: form.value.wantRefreshToken,
      cool_down_seconds: parseFloat(form.value.autoCoolDown) || 0,
      target_count: parseInt(form.value.autoTargetCount, 10) || 0,
      want_2fa: form.value.autoWant2fa,
      zooproxy_enabled: form.value.zooproxyEnabled,
      zooproxy_mode: form.value.zooproxyMode,
      zooproxy_api_url: form.value.zooproxyApiUrl,
      zooproxy_scheme: form.value.zooproxyScheme,
      zooproxy_gateway_host: form.value.zooproxyGatewayHost,
      zooproxy_gateway_account: form.value.zooproxyGatewayAccount,
      zooproxy_gateway_password: form.value.zooproxyGatewayPassword,
      zooproxy_gateway_region: form.value.zooproxyGatewayRegion,
      zooproxy_gateway_session_minutes: form.value.zooproxyGatewaySessionMinutes,
      ipwo_enabled: form.value.ipwoEnabled,
      ipwo_host: form.value.ipwoHost,
      ipwo_username: form.value.ipwoUsername,
      ipwo_password: form.value.ipwoPassword,
      ipwo_zone: form.value.ipwoZone,
      ipwo_scheme: form.value.ipwoScheme,
      ipwo_session_minutes: form.value.ipwoSessionMinutes,
      proxy_multi_country: form.value.proxyMultiCountry,
      proxy_countries_zoo: form.value.proxyCountriesZoo,
      proxy_countries_ipwo: form.value.proxyCountriesIpwo,
      proxy_dual_mode: form.value.proxyDualMode,
      auto_resume_on_network: form.value.autoResumeOnNetwork,
    })
    ElMessage.success('自动跑号已启动')
  } catch (e) { ElMessage.error('启动失败: ' + e.message) }
}
async function call(fn, name) {
  try { await fn(); ElMessage.success(name + ' 成功') }
  catch (e) { ElMessage.error(name + ' 失败: ' + e.message) }
}
</script>

<template>
  <div class="page">
    <el-card shadow="never" style="margin-bottom: 16px">
      <template #header><span class="section-title" style="margin: 0">全自动批量注册</span></template>

      <el-space wrap :size="16" style="margin-bottom: 12px">
        <el-form-item label="并发" style="margin: 0">
          <el-input-number v-model="form.autoConcurrency" :min="1" :max="100" />
        </el-form-item>
        <el-form-item label="冷却(秒)" style="margin: 0">
          <el-input-number v-model="form.autoCoolDown" :min="0" :max="120" />
        </el-form-item>
        <el-form-item label="目标数(0=不限)" style="margin: 0">
          <el-input-number v-model="form.autoTargetCount" :min="0" :max="100000" />
        </el-form-item>
        <el-form-item label="OTP 等待(秒)" style="margin: 0">
          <el-input-number v-model="form.otpTimeout" :min="10" :max="600" />
        </el-form-item>
      </el-space>

      <el-form-item label="2FA">
        <div style="display: flex; align-items: center; gap: 10px; flex-wrap: wrap">
          <el-switch v-model="form.autoWant2fa" />
          <span>每个号注册成功后自动绑定 2FA（TOTP）</span>
        </div>
        <div class="hint" style="margin-top: 6px; line-height: 1.5">
          默认开。绑定不可逆：之后该号所有登录都需 6 位动态码；
          secret 仅下发<b>一次</b>、服务端取不回，跑完请到「注册结果」页<b>导出备份</b>。
          绑定失败<b>不会废号</b>（仅日志告警、账号照常入库）；
          <b>无密码的号会自动跳过</b>，所以「每个号」实际是「每个有密码的号」。
        </div>
      </el-form-item>

      <el-form-item label="ZooProxy 动态 IP">
        <div style="display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin-bottom: 8px">
          <el-switch v-model="form.zooproxyEnabled" />
          <span>每号换出口 IP（推荐账号网关 SOCKS5，与白名单 API 不同）</span>
        </div>
        <el-radio-group v-model="form.zooproxyMode" :disabled="!form.zooproxyEnabled" style="margin-bottom: 8px">
          <el-radio value="gateway">账号网关 SOCKS5</el-radio>
          <el-radio value="api">白名单提取 API</el-radio>
        </el-radio-group>
        <template v-if="form.zooproxyMode === 'gateway'">
          <el-input v-model="form.zooproxyGatewayHost" :disabled="!form.zooproxyEnabled" class="mono" placeholder="us-eu.zooproxy.com:5000" style="margin-bottom: 6px" />
          <el-input v-model="form.zooproxyGatewayAccount" :disabled="!form.zooproxyEnabled" class="mono" placeholder="kt68crvmbh24937" style="margin-bottom: 6px" />
          <el-input v-model="form.zooproxyGatewayPassword" :disabled="!form.zooproxyEnabled" type="password" show-password placeholder="控制台密码" style="margin-bottom: 6px" />
          <el-space wrap>
            <el-form-item label="单国(关多国时用)" style="margin: 0">
              <el-input
                v-model="form.zooproxyGatewayRegion"
                :disabled="!form.zooproxyEnabled || form.proxyMultiCountry"
                style="width: 72px"
              />
            </el-form-item>
            <el-form-item label="轮换(分)" style="margin: 0">
              <el-input-number v-model="form.zooproxyGatewaySessionMinutes" :disabled="!form.zooproxyEnabled" :min="1" :max="120" />
            </el-form-item>
          </el-space>
          <div class="hint" style="margin-top: 6px">
            等同 <span class="mono">curl --socks5 host:5000 -U 账号-region-US-sid-随机-t-10:密码</span>；每号自动生成新 <span class="mono">sid</span>。
          </div>
        </template>
        <template v-else>
          <el-input
            v-model="form.zooproxyApiUrl"
            :disabled="!form.zooproxyEnabled"
            class="mono"
            placeholder="https://white.zooproxy.com/white/api?region=US&num=1&time=10&format=1&type=json"
          />
          <div class="hint" style="margin-top: 6px">VPS 出口须加白；部分节点 HTTPS 会 TLS35，不通请改网关模式。</div>
        </template>
        <div class="hint" style="margin-top: 6px">
          开启后忽略静态代理池。熔断约 45s 自动恢复（可关 <span class="mono">autoResumeOnNetwork</span>）。
        </div>
      </el-form-item>

      <el-form-item label="IPWO 动态 IP">
        <div style="display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin-bottom: 8px">
          <el-switch v-model="form.ipwoEnabled" />
          <span>账密网关 HTTP（矩阵 BR/DE/NA 较稳；可与 ZooProxy 双开分流 worker）</span>
        </div>
        <el-input v-model="form.ipwoHost" :disabled="!form.ipwoEnabled" class="mono" placeholder="us.ipwo.net:7878" style="margin-bottom: 6px" />
        <el-input v-model="form.ipwoUsername" :disabled="!form.ipwoEnabled" class="mono" placeholder="账号_custom_zone_NA" style="margin-bottom: 6px" />
        <el-input v-model="form.ipwoPassword" :disabled="!form.ipwoEnabled" type="password" show-password placeholder="密码可空" style="margin-bottom: 6px" />
        <el-space wrap>
          <el-form-item label="单国(关多国时用)" style="margin: 0">
            <el-input v-model="form.ipwoZone" :disabled="!form.ipwoEnabled || form.proxyMultiCountry" style="width: 72px" />
          </el-form-item>
          <el-form-item label="协议" style="margin: 0">
            <el-select v-model="form.ipwoScheme" :disabled="!form.ipwoEnabled" style="width: 100px">
              <el-option label="http" value="http" />
              <el-option label="socks5h" value="socks5h" />
            </el-select>
          </el-form-item>
          <el-form-item label="粘性(分)" style="margin: 0">
            <el-input-number v-model="form.ipwoSessionMinutes" :disabled="!form.ipwoEnabled" :min="1" :max="120" />
          </el-form-item>
        </el-space>
      </el-form-item>

      <el-form-item label="多国家代理池">
        <div style="display: flex; align-items: center; gap: 10px; margin-bottom: 8px">
          <el-switch v-model="form.proxyMultiCountry" :disabled="!form.zooproxyEnabled && !form.ipwoEnabled" />
          <span>按 worker / 轮次轮换国家（矩阵已排除 UK）</span>
        </div>
        <el-input
          v-model="form.proxyCountriesZoo"
          :disabled="!form.proxyMultiCountry || !form.zooproxyEnabled"
          class="mono"
          placeholder="ZooProxy: FR,JP,BR,CA,DE,NA,US"
          style="margin-bottom: 6px"
        />
        <el-input
          v-model="form.proxyCountriesIpwo"
          :disabled="!form.proxyMultiCountry || !form.ipwoEnabled"
          class="mono"
          placeholder="IPWO: BR,DE,NA,FR,JP,US,CA"
          style="margin-bottom: 6px"
        />
        <div v-if="form.zooproxyEnabled && form.ipwoEnabled" style="margin-top: 6px">
          <span class="hint">双提供商：</span>
          <el-radio-group v-model="form.proxyDualMode" size="small">
            <el-radio value="split_workers">偶数 worker Zoo / 奇数 IPWO</el-radio>
            <el-radio value="round_robin">每个 run 交替</el-radio>
          </el-radio-group>
        </div>
      </el-form-item>

      <el-form-item label="Codex refresh_token">
        <div style="display: flex; align-items: center; gap: 10px">
          <el-switch v-model="form.wantRefreshToken" />
          <span class="hint">关则跳过 Codex OAuth，只拿 session（ChatGPT2API 推荐关）</span>
        </div>
      </el-form-item>

      <el-form-item label="代理池">
        <div style="display: flex; align-items: center; gap: 12px; flex-wrap: wrap">
          <el-tag :type="proxyCount ? 'success' : 'info'" effect="light">
            当前 {{ proxyCount }} 个代理
          </el-tag>
          <span class="hint">
            {{ (form.zooproxyEnabled || form.ipwoEnabled) ? '已开动态代理，静态池不参与批量' : (proxyCount ? '各 worker 按顺序轮流取用' : '为空：所有 worker 用「单次注册」页填的单代理') }}
          </span>
          <el-button size="small" @click="router.push('/proxy')">管理代理池</el-button>
        </div>
      </el-form-item>

      <el-space wrap style="margin-top: 8px">
        <el-button type="primary" :disabled="!canStart" @click="start">开始</el-button>
        <el-button :disabled="!canPause" @click="call(autoPause, '暂停')">暂停</el-button>
        <el-button :disabled="!canResume" @click="call(autoResume, '恢复')">恢复</el-button>
        <el-button type="danger" :disabled="!canStop" @click="call(autoStop, '停止')">停止</el-button>
      </el-space>

      <el-descriptions :column="4" border size="small" style="margin-top: 16px">
        <el-descriptions-item label="状态"><StatusDot :type="stateType" :text="stateLabel" /></el-descriptions-item>
        <el-descriptions-item label="成功">
          <b style="color: var(--el-color-success)">{{ autoStatus.registered_ok || 0 }}</b>
          <span v-if="autoStatus.target_count"> / {{ autoStatus.target_count }}</span>
        </el-descriptions-item>
        <el-descriptions-item label="失败">
          <b style="color: var(--el-color-danger)">{{ autoStatus.registered_fail || 0 }}</b>
        </el-descriptions-item>
        <el-descriptions-item label="并发">{{ autoStatus.concurrency || 1 }}</el-descriptions-item>
      </el-descriptions>

      <div v-if="workers.length" style="margin-top: 12px">
        <el-tag v-for="w in workers" :key="w.id" type="warning" effect="plain" style="margin: 0 6px 6px 0">
          worker-{{ w.id }}
          <template v-if="w.proxy_label"> · <span class="mono">{{ w.proxy_label }}</span></template>
          · {{ w.email }}
        </el-tag>
      </div>
      <p v-if="autoStatus.last_message" class="hint" style="margin-top: 8px">{{ autoStatus.last_message }}</p>
    </el-card>

    <el-card shadow="never">
      <LogPanel />
    </el-card>
  </div>
</template>
