import http from './request'

// ──────────────── 邮箱来源配置 ────────────────
// 已注册的 provider 清单（含能力声明和配置项声明）。
// 页面据此动态渲染，后端加一种邮箱，这里和页面都不用改。
export const getMailProviders = (pooledOnly = false) =>
  http.get('/api/mail/providers', { params: { pooled_only: pooledOnly } })

export const getMailConfig = () => http.get('/api/settings/mail')
export const saveMailConfig = (payload) => http.post('/api/settings/mail', payload)
export const testMail = () => http.post('/api/settings/mail/test')

// ──────────────── SMS 接码配置 ────────────────
export const getSmsConfig = () => http.get('/api/settings/sms')
export const saveSmsConfig = (payload) => http.post('/api/settings/sms', payload)
export const testSms = () => http.post('/api/settings/sms/test')
export const getSmsTopCountries = () => http.get('/api/settings/sms/countries')
export const getSmsAllCountries = (provider = '') =>
  http.get('/api/settings/sms/all_countries', { params: { provider } })

// ──────────────── 推送渠道（push_targets） ────────────────
export const listPushTargets = () => http.get('/api/push-targets')
export const createPushTarget = (payload) => http.post('/api/push-targets', payload)
export const updatePushTarget = (id, payload) => http.put(`/api/push-targets/${id}`, payload)
export const deletePushTarget = (id) => http.delete(`/api/push-targets/${id}`)
export const testPushTarget = (id) => http.post(`/api/push-targets/${id}/test`)
export const retryPendingPush = (payload) => http.post('/api/push-targets/retry', payload)

// ──────────────── 自动导出配置 (CPA / SUB2API) ────────────────
export const getExportConfig = () => http.get('/api/settings/export')
export const saveExportConfig = (payload) => http.post('/api/settings/export', payload)
export const testExport = (target) => http.post('/api/settings/export/test', { target })
