import { defineStore } from 'pinia'
import { reactive, watch } from 'vue'
import http from '@/api/request'

const KEY = 'gpt_outlook_register_form_v3'

// 跨页面共享 + localStorage 持久化的表单字段
// （proxy 在 注册 / 自动跑号 / Plus 检测 三处共用）
const defaults = {
  proxy: '',
  otpTimeout: 180,
  autoConcurrency: 24,
  autoCoolDown: 5,
  autoTargetCount: 0,
  // 注册后自动绑 2FA。单次 / 批量都**默认 true**：每个号都要 2FA。
  // 仍然拆成两个字段（而不是共用一个）：单次页是验 bug / 试流程的测试台，
  // 共用的话在那边临时关掉，回头批量跑几百个号就全裸奔了。
  // localStorage 只记住主人上次的选择，不改变默认值：清缓存后两边都回到 true。
  want2fa: true,
  autoWant2fa: true,
  wantRefreshToken: false,
  zooproxyEnabled: true,
  zooproxyMode: 'gateway',
  zooproxyApiUrl:
    'https://white.zooproxy.com/white/api?region=US&num=1&time=10&format=1&type=json',
  zooproxyScheme: 'http',
  zooproxyGatewayHost: 'us-eu.zooproxy.com:5000',
  zooproxyGatewayAccount: 'kt68crvmbh24937',
  zooproxyGatewayPassword: '',
  zooproxyGatewayRegion: 'US',
  zooproxyGatewaySessionMinutes: 10,
  ipwoEnabled: true,
  ipwoHost: 'us.ipwo.net:7878',
  ipwoUsername: 'jieruiname1',
  ipwoPassword: '',
  ipwoZone: 'NA',
  ipwoScheme: 'http',
  ipwoSessionMinutes: 10,
  proxyMultiCountry: true,
  proxyCountriesZoo: 'FR,JP,BR,CA,DE,NA',
  proxyCountriesIpwo: 'BR,DE,NA,FR,JP',
  proxyDualMode: 'split_workers',
  autoResumeOnNetwork: true,
}

// el-select 的 clearable 清空时把值写成 **undefined**（不是 ''），而 proxy 在三个
// 页面都是 `form.value.proxy.trim()` 直接调 —— 主人点一次叉，下次提交就
// "Cannot read properties of undefined (reading 'trim')"。这里统一兜底成字符串，
// 免得每个调用点各写各的可选链，也顺手挡住 localStorage 里的历史脏值。
export function proxyText(form) {
  return String(form?.proxy ?? '').trim()
}

export const useFormStore = defineStore('form', () => {
  let saved = {}
  try { saved = JSON.parse(localStorage.getItem(KEY) || '{}') } catch (_) { saved = {} }
  const form = reactive({ ...defaults, ...saved })

  // clearable 清空后 proxy 会变成 undefined 并被持久化进 localStorage，
  // 刷新页面后依然是 undefined。这里watch 回填成 ''，保证存量数据也是干净的。
  watch(() => form.proxy, (v) => {
    if (v === undefined || v === null) form.proxy = ''
  })

  watch(form, (v) => {
    try { localStorage.setItem(KEY, JSON.stringify(v)) } catch (_) {}
  }, { deep: true })

  http.get('/api/settings/proxy_preset').then((res) => {
    const preset = res?.preset
    if (preset && typeof preset === 'object') {
      Object.assign(form, preset)
    }
  }).catch(() => {})

  return { form }
})
